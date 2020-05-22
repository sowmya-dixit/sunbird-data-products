package org.sunbird.analytics.job.report

import org.apache.commons.lang3.StringUtils
import org.apache.spark.SparkContext
import org.apache.spark.sql.DataFrame
import org.apache.spark.sql.SparkSession
import org.apache.spark.sql.expressions.Window
import org.apache.spark.sql.functions.ceil
import org.apache.spark.sql.functions.col
import org.apache.spark.sql.functions.collect_list
import org.apache.spark.sql.functions.concat
import org.apache.spark.sql.functions.concat_ws
import org.apache.spark.sql.functions.desc
import org.apache.spark.sql.functions.explode
import org.apache.spark.sql.functions.first
import org.apache.spark.sql.functions.lit
import org.apache.spark.sql.functions.lower
import org.apache.spark.sql.functions.row_number
import org.apache.spark.sql.functions.split
import org.apache.spark.sql.functions.sum
import org.ekstep.analytics.framework.FrameworkContext
import org.ekstep.analytics.framework.IJob
import org.ekstep.analytics.framework.JobConfig
import org.ekstep.analytics.framework.JobContext
import org.ekstep.analytics.framework.Level.ERROR
import org.ekstep.analytics.framework.Level.INFO
import org.ekstep.analytics.framework.util.DatasetUtil.extensions
import org.ekstep.analytics.framework.util.JSONUtils
import org.ekstep.analytics.framework.util.JobLogger
import org.ekstep.analytics.framework.util.{CommonUtil, JSONUtils, JobLogger}
import org.sunbird.analytics.util.ESUtil
import org.joda.time.DateTime
import org.joda.time.format.DateTimeFormat
import org.sunbird.cloud.storage.conf.AppConf

object AssessmentMetricsJob extends optional.Application with IJob with BaseReportsJob {

  implicit val className = "org.ekstep.analytics.job.AssessmentMetricsJob"

  private val indexName: String = AppConf.getConfig("assessment.metrics.es.index.prefix") + DateTimeFormat.forPattern("dd-MM-yyyy-HH-mm").print(DateTime.now())


  def name(): String = "AssessmentMetricsJob"

  def main(config: String)(implicit sc: Option[SparkContext] = None, fc: Option[FrameworkContext] = None) {


    JobLogger.init("Assessment Metrics")
    JobLogger.start("Assessment Job Started executing", Option(Map("config" -> config, "model" -> name)))
    val jobConfig = JSONUtils.deserialize[JobConfig](config)
    JobContext.parallelization = CommonUtil.getParallelization(jobConfig);
    JobLogger.log("parallelization" + JobContext.parallelization, None, INFO)

    implicit val sparkContext: SparkContext = getReportingSparkContext(jobConfig);
    implicit val frameworkContext: FrameworkContext = getReportingFrameworkContext();
    execute(jobConfig)
  }

  def recordTime[R](block: => R, msg: String): (R) = {
    val t0 = System.currentTimeMillis()
    val result = block
    val t1 = System.currentTimeMillis()
    JobLogger.log(msg + (t1 - t0), None, INFO)
    println(msg + (t1 - t0))
    result;
  }


  private def execute(config: JobConfig)(implicit sc: SparkContext, fc: FrameworkContext) = {
    val tempDir = AppConf.getConfig("assessment.metrics.temp.dir")
    val readConsistencyLevel: String = AppConf.getConfig("assessment.metrics.cassandra.input.consistency")
    val sparkConf = sc.getConf
      .set("spark.cassandra.input.consistency.level", readConsistencyLevel)
      .set("spark.sql.caseSensitive", AppConf.getConfig(key = "spark.sql.caseSensitive"))
    implicit val spark: SparkSession = SparkSession.builder.config(sparkConf).getOrCreate()
    val reportDF = recordTime(prepareReport(spark, loadData).cache(), s"Time take generate the dataframe} - ")
    val denormalizedDF = recordTime(denormAssessment(reportDF), s"Time take to denorm the assessment -")
    recordTime(saveReport(denormalizedDF, tempDir), s"Time take to save the all the reports into both blob and es -")
    reportDF.unpersist(true)
    JobLogger.end("AssessmentReport Generation Job completed successfully!", "SUCCESS", Option(Map("config" -> config, "model" -> name)))
    spark.stop()
  }

  /**
   * Method used to load the cassnadra table data by passing configurations
   *
   * @param spark    - Spark Sessions
   * @param settings - Cassnadra configs
   * @return
   */
  def loadData(spark: SparkSession, settings: Map[String, String]): DataFrame = {
    spark
      .read
      .format("org.apache.spark.sql.cassandra")
      .options(settings)
      .load()
  }

  /**
   * Loading the specific tables from the cassandra db.
   */
  def prepareReport(spark: SparkSession, loadData: (SparkSession, Map[String, String]) => DataFrame): DataFrame = {
    val sunbirdKeyspace = AppConf.getConfig("course.metrics.cassandra.sunbirdKeyspace")
    val sunbirdCoursesKeyspace = AppConf.getConfig("course.metrics.cassandra.sunbirdCoursesKeyspace")
    val courseBatchDF = loadData(spark, Map("table" -> "course_batch", "keyspace" -> sunbirdCoursesKeyspace)).select("courseid", "batchid", "enddate", "startdate")
    val userCoursesDF = loadData(spark, Map("table" -> "user_courses", "keyspace" -> sunbirdCoursesKeyspace))
      .filter(lower(col("active")).equalTo("true"))
      .select(col("batchid"), col("userid"), col("courseid"), col("active")
      , col("completionpercentage"), col("enrolleddate"), col("completedon"))
    val userDF = loadData(spark, Map("table" -> "user", "keyspace" -> sunbirdKeyspace)).select(col("userid"),
      col("maskedemail"),
      col("firstname"),
      col("lastname"),
      col("maskedphone"),
      col("rootorgid"),
      col("locationids"),
      col("channel")
    ).cache()
    val userOrgDF = loadData(spark, Map("table" -> "user_org", "keyspace" -> sunbirdKeyspace)).filter(lower(col("isdeleted")) === "false").select(col("userid"), col("organisationid")).cache()
    val organisationDF = loadData(spark, Map("table" -> "organisation", "keyspace" -> sunbirdKeyspace)).select(col("id"), col("orgname")).cache()
    val locationDF = loadData(spark, Map("table" -> "location", "keyspace" -> sunbirdKeyspace)).filter(col("type") === "district" || col("type") === "block")
      .select(col("id"), col("name"), col("type"))
    val externalIdentityDF = loadData(spark, Map("table" -> "usr_external_identity", "keyspace" -> sunbirdKeyspace)).select(col("provider"), col("idtype"), col("externalid"), col("userid")).cache()
    val assessmentProfileDF = loadData(spark, Map("table" -> "assessment_aggregator4", "keyspace" -> sunbirdCoursesKeyspace))
      .select("course_id", "batch_id", "user_id", "content_id", "total_max_score", "total_score", "grand_total")

    /*
    * courseBatchDF has details about the course and batch details for which we have to prepare the report
    * courseBatchDF is the primary source for the report
    * userCourseDF has details about the user details enrolled for a particular course/batch
    * */
    val userCourseDenormDF = courseBatchDF.join(userCoursesDF, userCoursesDF.col("batchid") === courseBatchDF.col("batchid"), "inner")
      .select(
        userCoursesDF.col("batchid"),
        col("userid"),
        col("active"),
        courseBatchDF.col("courseid"))

    /*
    *userCourseDenormDF lacks some of the user information that need to be part of the report
    *here, it will add some more user details
    * */
    val userDenormDF = userCourseDenormDF
      .join(userDF, Seq("userid"), "inner")
      .select(
        userCourseDenormDF.col("*"),
        col("firstname"),
        col("lastname"),
        col("maskedemail"),
        col("maskedphone"),
        col("rootorgid"),
        col("userid"),
        col("locationids"),
        concat_ws(" ", col("firstname"), col("lastname")).as("username"))
    /**
     * externalIdMapDF - Filter out the external id by idType and provider and Mapping userId and externalId
     */
    val externalIdMapDF = userDF.join(externalIdentityDF, externalIdentityDF.col("idtype") === userDF.col("channel") && externalIdentityDF.col("provider") === userDF.col("channel") && externalIdentityDF.col("userid") === userDF.col("userid"), "inner")
      .select(externalIdentityDF.col("externalid"), externalIdentityDF.col("userid"))

    /*
    * userDenormDF lacks organisation details, here we are mapping each users to get the organisationids
    * */
    val userRootOrgDF = userDenormDF
      .join(userOrgDF, userOrgDF.col("userid") === userDenormDF.col("userid") && userOrgDF.col("organisationid") === userDenormDF.col("rootorgid"))
      .select(userDenormDF.col("*"), col("organisationid"))

    val userSubOrgDF = userDenormDF
      .join(userOrgDF, userOrgDF.col("userid") === userDenormDF.col("userid") && userOrgDF.col("organisationid") =!= userDenormDF.col("rootorgid"))
      .select(userDenormDF.col("*"), col("organisationid"))

    val rootOnlyOrgDF = userRootOrgDF
      .join(userSubOrgDF, Seq("userid"), "leftanti")
      .select(userRootOrgDF.col("*"))

    val userOrgDenormDF = rootOnlyOrgDF.union(userSubOrgDF)

    /**
     * Get the District name for particular user based on the location identifiers
     */
    val locationDenormDF = userOrgDenormDF
      .withColumn("exploded_location", explode(col("locationids")))
      .join(locationDF, col("exploded_location") === locationDF.col("id") && locationDF.col("type") === "district")
      .dropDuplicates(Seq("userid"))
      .select(col("name").as("district_name"), col("userid"))

    val userLocationResolvedDF = userOrgDenormDF
      .join(locationDenormDF, Seq("userid"), "left_outer")

    val assessmentDF = getAssessmentData(assessmentProfileDF)
    //JobLogger.log("Total Assessment Data Count is" + assessmentDF.count(), None, INFO)

    /**
     * Compute the sum of all the worksheet contents score.
     */
    val assessmentAggDf = Window.partitionBy("user_id", "batch_id", "course_id")
    val resDF = assessmentDF
      .withColumn("agg_score", sum("total_score") over assessmentAggDf)
      .withColumn("agg_max_score", sum("total_max_score") over assessmentAggDf)
      .withColumn("total_sum_score", concat(ceil((col("agg_score") * 100) / col("agg_max_score")), lit("%")))
    /**
     * Filter only valid enrolled userid for the specific courseid
     */
    val userAssessmentResolvedDF = userLocationResolvedDF.join(resDF, userLocationResolvedDF.col("userid") === resDF.col("user_id") && userLocationResolvedDF.col("batchid") === resDF.col("batch_id") && userLocationResolvedDF.col("courseid") === resDF.col("course_id"), "right_outer")
    val resolvedExternalIdDF = userAssessmentResolvedDF.join(externalIdMapDF, Seq("userid"), "left_outer")

    /*
    * Resolve organisation name from `rootorgid`
    * */
    val resolvedOrgNameDF = resolvedExternalIdDF
      .join(organisationDF, organisationDF.col("id") === resolvedExternalIdDF.col("rootorgid"), "left_outer")
      .dropDuplicates(Seq("userid"))
      .select(resolvedExternalIdDF.col("userid"), col("orgname").as("orgname_resolved"))

    /*
    * Resolve school name from `orgid`
    * */
    val resolvedSchoolNameDF = resolvedExternalIdDF
      .join(organisationDF, organisationDF.col("id") === resolvedExternalIdDF.col("organisationid"), "left_outer")
      .dropDuplicates(Seq("userid"))
      .select(resolvedExternalIdDF.col("userid"), col("orgname").as("schoolname_resolved"))

    /*
    * merge orgName and schoolName based on `userid` and calculate the course progress percentage from `progress` column which is no of content visited/read
    * */

    resolvedExternalIdDF
      .join(resolvedSchoolNameDF, Seq("userid"), "left_outer")
      .join(resolvedOrgNameDF, Seq("userid"), "left_outer")
  }

  /**
   * De-norming the assessment report - Adding content name column to the content id
   *
   * @return - Assessment denormalised dataframe
   */
  def denormAssessment(report: DataFrame)(implicit spark: SparkSession): DataFrame = {
    val contentIds: List[String] = recordTime(report.select(col("content_id")).distinct().collect().map(_ (0)).toList.asInstanceOf[List[String]], "Time taken to get the content IDs- ")
    val contentMetaDataDF = ESUtil.getAssessmentNames(spark, contentIds, AppConf.getConfig("assessment.metrics.content.index"), AppConf.getConfig("assessment.metrics.supported.contenttype"))
    report.join(contentMetaDataDF, report.col("content_id") === contentMetaDataDF.col("identifier"), "right_outer") // Doing right join since to generate report only for the "SelfAssess" content types
      .select(
        col("name").as("content_name"),
        col("total_sum_score"), report.col("userid"), report.col("courseid"), report.col("batchid"),
        col("grand_total"), report.col("maskedemail"), report.col("district_name"), report.col("maskedphone"),
        report.col("orgname_resolved"), report.col("externalid"), report.col("schoolname_resolved"), report.col("username"))
  }


  /**
   * This method is used to upload the report the azure cloud service and
   * Index report data into core elastic search.
   * Alias name: cbatch-assessment
   * Index name: cbatch-assessment-24-08-1993-09-30 (dd-mm-yyyy-hh-mm)
   */
  def saveReport(reportDF: DataFrame, url: String)(implicit spark: SparkSession, fc: FrameworkContext): Unit = {
    // Save the report to azure cloud storage
    val result = reportDF.groupBy("courseid").agg(collect_list("batchid").as("batchid"))
    val uploadToAzure = AppConf.getConfig("course.upload.reports.enabled")
    if (StringUtils.isNotBlank(uploadToAzure) && StringUtils.equalsIgnoreCase("true", uploadToAzure)) {
      val courseBatchList = result.collect.map(r => Map(result.columns.zip(r.toSeq): _*))
      save(courseBatchList, reportDF, url, spark)
    } else {
      JobLogger.log("Skipping uploading reports into to azure", None, INFO)
    }
  }

  /**
   * Converting rows into  column (Reshaping the dataframe.)
   * This method converts the name column into header row formate
   * Example:
   * Input DF
   * +------------------+-------+--------------------+-------+-----------+
   * |              name| userid|            courseid|batchid|total_score|
   * +------------------+-------+--------------------+-------+-----------+
   * |Playingwithnumbers|user021|do_21231014887798...|   1001|         10|
   * |     Whole Numbers|user021|do_21231014887798...|   1001|          4|
   * +------------------+---------------+-------+--------------------+----
   *
   * Output DF: After re-shaping the data frame.
   * +--------------------+-------+-------+------------------+-------------+
   * |            courseid|batchid| userid|Playingwithnumbers|Whole Numbers|
   * +--------------------+-------+-------+------------------+-------------+
   * |do_21231014887798...|   1001|user021|                10|            4|
   * +--------------------+-------+-------+------------------+-------------+
   * Example:
   */
  def transposeDF(reportDF: DataFrame): DataFrame = {
    // Re-shape the dataFrame (Convert the content name from the row to column)

    val reshapedDF = reportDF.groupBy("courseid", "batchid", "userid").pivot("content_name").agg(concat(ceil((split(first("grand_total"), "\\/").getItem(0) * 100) / (split(first("grand_total"), "\\/").getItem(1))), lit("%")))
    reshapedDF.join(reportDF, Seq("courseid", "batchid", "userid"), "inner").
      select(
        reportDF.col("externalid").as("External ID"),
        reportDF.col("userid").as("User ID"),
        reportDF.col("username").as("User Name"),
        reportDF.col("maskedemail").as("Email ID"),
        reportDF.col("maskedphone").as("Mobile Number"),
        reportDF.col("orgname_resolved").as("Organisation Name"),
        reportDF.col("district_name").as("District Name"),
        reportDF.col("schoolname_resolved").as("School Name"),
        reshapedDF.col("*"), // Since we don't know the content name column so we are using col("*")
        reportDF.col("total_sum_score").as("Total Score")).dropDuplicates("userid", "courseid", "batchid").drop("userid", "courseid", "batchid")
  }

  /**
   * Get the Either last updated assessment question or Best attempt assessment
   *
   * @param bestAttemptScore - Boolean, To get the best attempt score
   * @param reportDF         - Dataframe, Report df.
   * @return DataFrame
   */
  def getAssessmentData(reportDF: DataFrame): DataFrame = {
    val bestScoreReport = AppConf.getConfig("assessment.metrics.bestscore.report").toBoolean
    val columnName: String = if (bestScoreReport) "total_score" else "last_attempted_on"
    val df = Window.partitionBy("user_id", "batch_id", "course_id", "content_id").orderBy(desc(columnName))

    reportDF.withColumn("rownum", row_number.over(df)).where(col("rownum") === 1).drop("rownum")
  }

  def saveToAzure(reportDF: DataFrame, url: String, batchId: String): String = {
    val tempDir = AppConf.getConfig("assessment.metrics.temp.dir")
    val renamedDir = s"$tempDir/renamed"
    val storageConfig = getStorageConfig(AppConf.getConfig("cloud.container.reports"), AppConf.getConfig("assessment.metrics.cloud.objectKey"))
    reportDF.saveToBlobStore(storageConfig, "csv", "report-" + batchId, Option(Map("header" -> "true")), None);
    s"${AppConf.getConfig("cloud.container.reports")}/${AppConf.getConfig("assessment.metrics.cloud.objectKey")}/report-$batchId.csv"
  }

  def saveToElastic(index: String, reportDF: DataFrame): Unit = {
    val assessmentReportDF = reportDF.select(
      col("userid").as("userId"),
      col("username").as("userName"),
      col("courseid").as("courseId"),
      col("batchid").as("batchId"),
      col("grand_total").as("score"),
      col("maskedemail").as("maskedEmail"),
      col("maskedphone").as("maskedPhone"),
      col("district_name").as("districtName"),
      col("orgname_resolved").as("rootOrgName"),
      col("externalid").as("externalId"),
      col("schoolname_resolved").as("subOrgName"),
      col("total_sum_score").as("totalScore"),
      col("content_name").as("contentName"),
      col("reportUrl").as("reportUrl")
    )
    ESUtil.saveToIndex(assessmentReportDF, index)
  }

  def rollOverIndex(index: String, alias: String): Unit = {
    val indexList = ESUtil.getIndexName(alias)
    if (!indexList.contains(index)) ESUtil.rolloverIndex(index, alias)
  }


  def save(courseBatchList: Array[Map[String, Any]], reportDF: DataFrame, url: String, spark: SparkSession)(implicit fc: FrameworkContext): Unit = {
    val aliasName = AppConf.getConfig("assessment.metrics.es.alias")
    val indexToEs = AppConf.getConfig("course.es.index.enabled")
    courseBatchList.foreach(item => {

      val courseId = item.getOrElse("courseid", "").asInstanceOf[String]
      val batchList = item.getOrElse("batchid", "").asInstanceOf[Seq[String]].distinct
      JobLogger.log(s"Course batch mappings- courseId: $courseId and batchIdList is $batchList " + item, None, INFO)
      batchList.foreach(batchId => {
        if (!courseId.isEmpty && !batchId.isEmpty) {
          val filteredDF = reportDF.filter(col("courseid") === courseId && col("batchid") === batchId)
          val reportData = recordTime(transposeDF(filteredDF), s"Time take to transpose the $batchId DF -")
          JobLogger.log("Total report Data is" + reportData.count(), None, INFO)
          try {
            val urlBatch: String = recordTime(saveToAzure(reportData, url, batchId), s"Time taken to save the $batchId into azure -")
            val resolvedDF = filteredDF.withColumn("reportUrl", lit(urlBatch))
            if (StringUtils.isNotBlank(indexToEs) && StringUtils.equalsIgnoreCase("true", indexToEs)) {
              recordTime(saveToElastic(this.getIndexName, resolvedDF), s"Time taken to save the $batchId into to es -")
              JobLogger.log("Indexing of assessment report data is success: " + this.getIndexName, None, INFO)
            } else {
              JobLogger.log("Skipping Indexing assessment report into ES", None, INFO)
            }
          } catch {
            case e: Exception => JobLogger.log("File upload is failed due to " + e, None, ERROR)
          }
        } else {
          JobLogger.log("Report failed to create since course_id is " + courseId + "and batch_id is " + batchId, None, ERROR)
        }
      })
    })
    rollOverIndex(getIndexName, aliasName)
  }

  def getIndexName: String = {
    this.indexName
  }
}
