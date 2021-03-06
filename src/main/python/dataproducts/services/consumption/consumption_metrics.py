"""
Generate daily consumption metrics from blob storage
"""
import json, time
import os
import sys
import pdb
import requests
import findspark
import pandas as pd

from string import Template
from datetime import date, timedelta, datetime
from pathlib import Path
from pyspark.sql import SparkSession
from pyspark.sql import functions as func

from dataproducts.util.utils import create_json, post_data_to_blob, get_data_from_blob, \
    get_tenant_info, get_textbook_snapshot, push_metric_event
from dataproducts.resources.queries import dialcode_scans, content_downloads, \
    app_sessions_devices, app_plays

class DailyMetrics:
    def __init__(self, data_store_location, org_search, druid_hostname, content_search,
                 content_hierarchy, execution_date):
        self.data_store_location = Path(data_store_location)
        self.org_search = org_search
        self.druid_hostname = druid_hostname
        self.content_search = content_search
        self.content_hierarchy = content_hierarchy
        self.execution_date = execution_date
        self.config = {}


    def get_paginated_result(self, query):
        try:
            headers = {
                'Content-Type': "application/json"
            }
            url = "{}druid/v2/".format(self.druid_hostname)
            response = requests.request("POST", url, data=json.dumps(query), headers=headers)
            result = response.json()
            response_df = pd.DataFrame()
            while result[0]['result']['events']:
                data = [event['event'] for segment in result for event in segment['result']['events']]
                response_df = pd.concat([response_df, pd.DataFrame(data).drop(['timestamp'], axis=1)])
                query['pagingSpec']['pagingIdentifiers'] = result[0]['result']['pagingIdentifiers']
                response = requests.request("POST", url, data=json.dumps(query), headers=headers)
                result = response.json()
                if response_df.count().edata_filters_dialcodes > 10000:
                    return response_df
            response_df = response_df.reset_index().drop(['index'], axis=1)
            return response_df

        except Exception as e:
            raise Exception('Pagination failed! :: {}'.format(str(e)))


    # TODO: Compute Downloads using SHARE-In events
    def downloads(self, result_loc_, date_):
        """
        Compute daily content downloads by channel
        :param result_loc_: pathlib.Path object to store resultant CSV at.
        :param date_: datetime object to pass in query and path
        :return: None
        """
        end_date = date_ + timedelta(days=1)
        query = Template(content_downloads.init())
        query = query.substitute(app=self.config['context']['pdata']['id']['app'],
                                 start_date=datetime.strftime(date_, '%Y-%m-%dT00:00:00+00:00'),
                                 end_date=datetime.strftime(end_date, '%Y-%m-%dT00:00:00+00:00'))

        headers = {
            'Content-Type': "application/json"
        }
        url = "{}druid/v2/".format(self.druid_hostname)
        response = requests.request("POST", url, data=query, headers=headers)
        records = [events['event'] for events in response.json()]
        data = pd.DataFrame(records)
        content = pd.read_csv(str(result_loc_.parent.joinpath('tb_metadata', date_.strftime('%Y-%m-%d'), 'textbook_snapshot.csv')))
        content = content[content['contentType']=='Resource']
        content = content[['identifier', 'channel']]
        content.drop_duplicates(inplace=True)
        content.rename(columns={'identifier': 'object_id'}, inplace=True)
        data = data.merge(content, on="object_id", how="left")
        data = data[data['channel'].notnull()]
        download_counts = data.groupby('channel').sum()
        download_counts.reset_index(inplace=True)

        result_loc_.joinpath(date_.strftime('%Y-%m-%d')).mkdir(exist_ok=True)
        download_counts.to_csv(result_loc_.joinpath(date_.strftime('%Y-%m-%d'), 'downloads.csv'), index=False)
        post_data_to_blob(result_loc_.joinpath(date_.strftime('%Y-%m-%d'), 'downloads.csv'), backup=True)


    def app_and_plays(self, result_loc_, date_):
        """
        Compute App Sessions and content play sessions and time spent on content consumption.
        :param result_loc_: pathlib.Path object to store resultant CSV at.
        :param date_: datetime object to use in query and path
        :return: None
        """
        # Overall app session metrics
        end_date = date_ + timedelta(days=1)
        query = Template(app_sessions_devices.init())
        query = query.substitute(app=self.config['context']['pdata']['id']['app'],
                                 start_date=datetime.strftime(date_, '%Y-%m-%dT00:00:00+00:00'),
                                 end_date=datetime.strftime(end_date, '%Y-%m-%dT00:00:00+00:00'))
        headers = {
            'Content-Type': "application/json"
        }
        url = "{}druid/v2/".format(self.druid_hostname)
        response = requests.request("POST", url, data=query, headers=headers)
        records = [events['event'] for events in response.json()]
        app_df = pd.DataFrame(records)
        app_df['Total Devices on App'] = app_df['Total Devices on App'].astype(int)
        app_df['Total Time on App (in hours)'] = app_df['Total Time on App'] / 3600
        app_df.drop(['Total Time on App'], axis=1, inplace=True)
        result_loc_.joinpath(date_.strftime('%Y-%m-%d')).mkdir(exist_ok=True)
        app_df.to_csv(result_loc_.joinpath(date_.strftime('%Y-%m-%d'), 'app_sessions.csv'), index=False)
        post_data_to_blob(result_loc_.joinpath(date_.strftime('%Y-%m-%d'), 'app_sessions.csv'), backup=True)

        # Content Play and time spent
        query = Template(app_plays.init())
        query = query.substitute(app=self.config['context']['pdata']['id']['app'],
                                 portal=self.config['context']['pdata']['id']['portal'],
                                 start_date=datetime.strftime(date_, '%Y-%m-%dT00:00:00+00:00'),
                                 end_date=datetime.strftime(end_date, '%Y-%m-%dT00:00:00+00:00'))
        headers = {
            'Content-Type': "application/json"
        }
        url = "{}druid/v2/".format(self.druid_hostname)
        response = requests.request("POST", url, data=query, headers=headers)
        records = [events['event'] for events in response.json()]
        play_df = pd.DataFrame(records)
        
        content = pd.read_csv(str(result_loc_.parent.joinpath('tb_metadata', date_.strftime('%Y-%m-%d'), 'textbook_snapshot.csv')))
        content = content[['identifier', 'channel']]
        content.drop_duplicates(inplace=True)
        content.rename(columns={'identifier': 'object_rollup_l1'}, inplace=True)
        play_df = play_df.merge(content, on="object_rollup_l1", how="left")
        play_df.rename(columns={'dimensions_pdata_id': 'pdata_id', 'dimensions_did': 'Total Devices that played content'}, inplace=True)
        play_df = play_df.groupby(['channel', 'pdata_id']).agg({
                                    'Total Devices that played content': pd.Series.nunique,
                                    'Total Content Plays': pd.Series.sum,
                                    'Content Play Time': pd.Series.sum
                                })
        play_df.reset_index(inplace=True)
        play_df['Content Play Time (in hours)'] = play_df['Content Play Time'] / 3600
        play_df.drop(['Content Play Time'], axis=1, inplace=True)
        x_play = play_df.pivot(index='channel', columns='pdata_id')
        x_play.to_csv(result_loc_.joinpath(date_.strftime('%Y-%m-%d'), 'plays.csv'))
        post_data_to_blob(result_loc_.joinpath(date_.strftime('%Y-%m-%d'), 'plays.csv'), backup=True)


    def dialscans(self, result_loc_, date_):
        """
        compute failed/successful scans by channel
        :param result_loc_: pathlib.Path object to store resultant CSV at.
        :param date_: datetime object to use in query and path
        :return: None
        """
        end_date = date_ + timedelta(days=1)
        query = Template(dialcode_scans.init())
        query = query.substitute(app=self.config['context']['pdata']['id']['app'],
                                 portal=self.config['context']['pdata']['id']['portal'],
                                 start_date=datetime.strftime(date_, '%Y-%m-%dT00:00:00+00:00'),
                                 end_date=datetime.strftime(end_date, '%Y-%m-%dT00:00:00+00:00'))

        headers = {
            'Content-Type': "application/json"
        }
        url = "{}druid/v2/".format(self.druid_hostname)
        response = requests.request("POST", url, data=query, headers=headers)
        result = response.json()
        records = [events['event'] for events in result]
        data = pd.DataFrame(records)
        data['dialcode_channel'] = data.get('dialcode_channel', pd.Series(index=data.index, name='dialcode_channel'))
        data['dialcode_channel'] = data['dialcode_channel'].fillna("")
        data['failed_flag'] = pd.np.where(data['edata_size'].astype(int) > 0, 'Successful QR Scans', 'Failed QR Scans')
        df = data.groupby(['dialcode_channel', 'failed_flag']).sum()
        df = df.reset_index()[['dialcode_channel', 'failed_flag', 'count']]
        result_loc_.joinpath(date_.strftime('%Y-%m-%d')).mkdir(exist_ok=True)
        df.to_csv(result_loc_.joinpath(date_.strftime('%Y-%m-%d'), 'dial_scans.csv'), index=False)
        post_data_to_blob(result_loc_.joinpath(date_.strftime('%Y-%m-%d'), 'dial_scans.csv'), backup=True)

    def daily_metrics(self, read_loc_, date_):
        """
        merge the three metrics
        :param read_loc_: pathlib.Path object to read CSV from.
        :param date_: datetime object to use in path
        :return: None
        """
        try:
            board_slug = \
                pd.read_csv(
                    self.data_store_location.joinpath('textbook_reports', date_.strftime('%Y-%m-%d'), 'tenant_info.csv'))[
                    ['id', 'slug']]
            board_slug.set_index('id', inplace=True)
        except Exception:
            raise Exception('Board Slug Error!')
        try:
            scans_df = pd.read_csv(
                read_loc_.joinpath('dialcode_scans', date_.strftime('%Y-%m-%d'), 'dial_scans.csv')).fillna('')
            scans_df = scans_df.pivot(index='dialcode_channel', columns='failed_flag', values='count').reset_index().fillna(
                0)
            scans_df = scans_df.join(board_slug, on='dialcode_channel', how='left')[
                ['slug', 'Failed QR Scans', 'Successful QR Scans']]
            scans_df['Total QR scans'] = scans_df['Successful QR Scans'] + scans_df['Failed QR Scans']
            scans_df['Percentage (%) of Failed QR Scans'] = scans_df['Failed QR Scans'] * 100 / scans_df['Total QR scans']
            unmapped = scans_df[scans_df.slug.isna()]['Total QR scans'][0]
            scans_df.dropna(subset=['slug'], inplace=True)
        except Exception as e:
            raise Exception('Scans Error! :: {}'.format(str(e)))
        try:
            downloads_df = pd.read_csv(read_loc_.joinpath('downloads', date_.strftime('%Y-%m-%d'), 'downloads.csv'))
            downloads_df = downloads_df.fillna('').join(board_slug, on='channel', how='left')[['count', 'slug']].dropna(
                subset=['slug'])
            downloads_df.columns = ['Total Content Downloads', 'slug']
        except Exception:
            raise Exception('Downloads Error!')
        try:
            app_df = pd.read_csv(read_loc_.joinpath('play', date_.strftime('%Y-%m-%d'), 'app_sessions.csv'))
            app_df = app_df[['Total App Sessions', 'Total Devices on App', 'Total Time on App (in hours)']]
            plays_df = pd.read_csv(read_loc_.joinpath('play', date_.strftime('%Y-%m-%d'), 'plays.csv'), header=[0, 1], dtype={0: str})

            # Making the channel column as index with string type since the csv is in multiindex format
            plays_df.set_index(plays_df.columns[0], inplace=True)
            plays_df.index.names = ['channel']
            plays_df = plays_df[1:]

            plays_df = plays_df.reset_index().join(board_slug, on='channel', how='left')
            plays_df['Total Content Plays on App'] = plays_df.get(
                ('Total Content Plays', self.config['context']['pdata']['id']['app']),
                pd.Series(index=plays_df.index, name=('Total Content Plays', self.config['context']['pdata']['id']['app'])))
            plays_df['Total Content Plays on Portal'] = plays_df.get(
                ('Total Content Plays', self.config['context']['pdata']['id']['portal']),
                pd.Series(index=plays_df.index, name=('Total Content Plays', self.config['context']['pdata']['id']['portal'])))
            plays_df['Total Devices that played content on App'] = plays_df.get(
                ('Total Devices that played content', self.config['context']['pdata']['id']['app']),
                pd.Series(index=plays_df.index, name=('Total Devices that played content', self.config['context']['pdata']['id']['app'])))
            plays_df['Total Devices that played content on Portal'] = plays_df.get(
                ('Total Devices that played content', self.config['context']['pdata']['id']['portal']),
                pd.Series(index=plays_df.index, name=('Total Devices that played content', self.config['context']['pdata']['id']['portal'])))
            plays_df['Content Play Time on App (in hours)'] = plays_df.get(
                ('Content Play Time (in hours)', self.config['context']['pdata']['id']['app']),
                pd.Series(index=plays_df.index, name=('Content Play Time (in hours)', self.config['context']['pdata']['id']['app'])))
            plays_df['Content Play Time on Portal (in hours)'] = plays_df.get(
                ('Content Play Time (in hours)', self.config['context']['pdata']['id']['portal']),
                pd.Series(index=plays_df.index, name=('Content Play Time (in hours)', self.config['context']['pdata']['id']['portal'])))
            plays_df = plays_df[['Total Content Plays on App',
                                'Total Content Plays on Portal', 'Total Devices that played content on App',
                                'Total Devices that played content on Portal',
                                'Content Play Time on App (in hours)', 'Content Play Time on Portal (in hours)', 'slug']].dropna(
                                subset=['slug'])
        except Exception as e:
            raise Exception('App and Plays Error! :: {}'.format(str(e)))
        try:
            daily_metrics_df = scans_df.join(
                downloads_df.set_index('slug'), on='slug', how='outer'
            ).reset_index(drop=True).join(
                plays_df.set_index('slug'), on='slug', how='outer', rsuffix='_plays'
            ).fillna(0)
            daily_metrics_df['Date'] = '-'.join(date_.strftime('%Y-%m-%d').split('-')[::-1])
        except Exception:
            raise Exception('Daily Metrics Error!')
        try:
            overall = daily_metrics_df[
                ['Successful QR Scans', 'Failed QR Scans', 'Total Content Downloads', 'Total Content Plays on App',
                 'Total Content Plays on Portal', 'Total Devices that played content on App',
                 'Total Devices that played content on Portal',
                 'Content Play Time on App (in hours)', 'Content Play Time on Portal (in hours)']].sum().astype(int)
            overall['Total App Sessions'] = app_df['Total App Sessions'].loc[0]
            overall['Total Devices on App'] = app_df['Total Devices on App'].loc[0]
            overall['Total Time on App (in hours)'] = app_df['Total Time on App (in hours)'].loc[0]
            overall['Date'] = '-'.join(date_.strftime('%Y-%m-%d').split('-')[::-1])
            overall['Unmapped QR Scans'] = unmapped
            overall['Total QR scans'] = overall['Successful QR Scans'] + overall['Failed QR Scans'] + overall[
                'Unmapped QR Scans']
            overall['Percentage (%) of Failed QR Scans'] = '%.2f' % (
                    overall['Failed QR Scans'] * 100 / overall['Total QR scans'])
            overall['Percentage (%) of Unmapped QR Scans'] = '%.2f' % (
                    overall['Unmapped QR Scans'] * 100 / overall['Total QR scans'])
            overall['Total Content Plays'] = overall['Total Content Plays on App'] + overall[
                'Total Content Plays on Portal']
            overall['Total Devices that played content'] = overall['Total Devices that played content on App'] + overall[
                'Total Devices that played content on Portal']
            overall['Total Content Play Time (in hours)'] = overall['Content Play Time on App (in hours)'] + overall[
                'Content Play Time on Portal (in hours)']
            overall = overall[['Date', 'Total QR scans', 'Successful QR Scans', 'Failed QR Scans', 'Unmapped QR Scans',
                               'Percentage (%) of Failed QR Scans', 'Percentage (%) of Unmapped QR Scans',
                               'Total Content Downloads', 'Total App Sessions', 'Total Devices on App',
                               'Total Time on App (in hours)', 'Total Content Plays on App',
                               'Total Devices that played content on App',
                               'Content Play Time on App (in hours)',
                               'Total Content Plays on Portal',
                               'Total Devices that played content on Portal',
                               'Content Play Time on Portal (in hours)',
                               'Total Content Plays', 'Total Devices that played content',
                               'Total Content Play Time (in hours)'
                               ]]
            read_loc_.joinpath('portal_dashboards', 'overall').mkdir(exist_ok=True)
            read_loc_.joinpath('portal_dashboards', 'mhrd').mkdir(exist_ok=True)
            try:
                get_data_from_blob(read_loc_.joinpath('portal_dashboards', 'overall', 'daily_metrics.csv'))
                blob_data = pd.read_csv(read_loc_.joinpath('portal_dashboards', 'overall', 'daily_metrics.csv'))
            except:
                blob_data = pd.DataFrame()
            blob_data = blob_data.append(pd.DataFrame(overall).transpose(), sort=False).fillna('')
            blob_data.index = pd.to_datetime(blob_data.Date, format='%d-%m-%Y')
            blob_data.drop_duplicates('Date', inplace=True, keep='last')
            blob_data.sort_index(inplace=True)
            # can remove after first run
            blob_data = blob_data[['Date', 'Total QR scans', 'Successful QR Scans', 'Failed QR Scans',
                                   'Unmapped QR Scans', 'Percentage (%) of Failed QR Scans',
                                   'Percentage (%) of Unmapped QR Scans', 'Total Content Downloads',
                                   'Total App Sessions', 'Total Devices on App',
                                   'Total Time on App (in hours)', 'Total Content Plays on App',
                                   'Total Devices that played content on App',
                                   'Content Play Time on App (in hours)', 'Total Content Plays on Portal',
                                   'Total Devices that played content on Portal',
                                   'Content Play Time on Portal (in hours)', 'Total Content Plays',
                                   'Total Devices that played content', 'Total Content Play Time (in hours)']]
            blob_data.to_csv(read_loc_.joinpath('portal_dashboards', 'overall', 'daily_metrics.csv'), index=False)
            create_json(read_loc_.joinpath('portal_dashboards', 'overall', 'daily_metrics.csv'))
            post_data_to_blob(read_loc_.joinpath('portal_dashboards', 'overall', 'daily_metrics.csv'))
        except Exception:
            raise Exception('Overall Metrics Error!')
        try:
            daily_metrics_df['Total Content Plays'] = daily_metrics_df['Total Content Plays on App'] + daily_metrics_df[
                'Total Content Plays on Portal']
            daily_metrics_df['Total Devices that played content'] = daily_metrics_df[
                                                                        'Total Devices that played content on App'] + \
                                                                    daily_metrics_df[
                                                                        'Total Devices that played content on Portal']
            daily_metrics_df['Total Content Play Time (in hours)'] = daily_metrics_df[
                                                                         'Content Play Time on App (in hours)'] + \
                                                                     daily_metrics_df[
                                                                         'Content Play Time on Portal (in hours)']
            daily_metrics_df.set_index(['slug'], inplace=True)
            daily_metrics_df = daily_metrics_df[['Date', 'Total QR scans', 'Successful QR Scans', 'Failed QR Scans',
                                                 'Percentage (%) of Failed QR Scans', 'Total Content Downloads',
                                                 'Total Content Plays on App',
                                                 'Total Devices that played content on App',
                                                 'Content Play Time on App (in hours)',
                                                 'Total Content Plays on Portal',
                                                 'Total Devices that played content on Portal',
                                                 'Content Play Time on Portal (in hours)',
                                                 'Total Content Plays', 'Total Devices that played content',
                                                 'Total Content Play Time (in hours)']]
            for slug, value in daily_metrics_df.iterrows():
                if slug != '':
                    read_loc_.joinpath('portal_dashboards', slug).mkdir(exist_ok=True)
                    for key, val in value.items():
                        if key not in ['Date', 'Percentage (%) of Failed QR Scans']:
                            value[key] = int(val)
                        elif key == 'Percentage (%) of Failed QR Scans':
                            value[key] = '%.2f' % val
                    try:
                        get_data_from_blob(read_loc_.joinpath('portal_dashboards', slug, 'daily_metrics.csv'))
                        blob_data = pd.read_csv(read_loc_.joinpath('portal_dashboards', slug, 'daily_metrics.csv'))
                    except:
                        blob_data = pd.DataFrame()
                    blob_data = blob_data.append(pd.DataFrame(value).transpose(), sort=False).fillna('')
                    blob_data.index = pd.to_datetime(blob_data.Date, format='%d-%m-%Y')
                    blob_data.drop_duplicates('Date', inplace=True, keep='last')
                    blob_data.sort_index(inplace=True)
                    # can remove after first run
                    blob_data = blob_data[['Date', 'Total QR scans', 'Successful QR Scans', 'Failed QR Scans',
                                           'Percentage (%) of Failed QR Scans', 'Total Content Downloads',
                                           'Total Content Plays on App',
                                           'Total Devices that played content on App',
                                           'Content Play Time on App (in hours)', 'Total Content Plays on Portal',
                                           'Total Devices that played content on Portal',
                                           'Content Play Time on Portal (in hours)', 'Total Content Plays',
                                           'Total Devices that played content', 'Total Content Play Time (in hours)']]
                    blob_data.to_csv(read_loc_.joinpath('portal_dashboards', slug, 'daily_metrics.csv'), index=False)
                    create_json(read_loc_.joinpath('portal_dashboards', slug, 'daily_metrics.csv'))
                    post_data_to_blob(read_loc_.joinpath('portal_dashboards', slug, 'daily_metrics.csv'))
        except Exception:
            raise Exception('State Metrics Error!')


    def init(self):
        start_time_sec = int(round(time.time()))
        start_time = datetime.now()
        print("Started at: ", start_time.strftime('%Y-%m-%d %H:%M:%S'))
        findspark.init()
        execution_date = datetime.strptime(self.execution_date, "%d/%m/%Y")
        analysis_date = execution_date - timedelta(1)

        self.data_store_location.joinpath('tb_metadata').mkdir(exist_ok=True)
        self.data_store_location.joinpath('play').mkdir(exist_ok=True)
        self.data_store_location.joinpath('downloads').mkdir(exist_ok=True)
        self.data_store_location.joinpath('dialcode_scans').mkdir(exist_ok=True)
        self.data_store_location.joinpath('portal_dashboards').mkdir(exist_ok=True)
        self.data_store_location.joinpath('config').mkdir(exist_ok=True)
        get_data_from_blob(self.data_store_location.joinpath('config', 'diksha_config.json'))
        with open(self.data_store_location.joinpath('config', 'diksha_config.json'), 'r') as f:
            self.config = json.loads(f.read())
        get_textbook_snapshot(result_loc_=self.data_store_location.joinpath('tb_metadata'), content_search_=self.content_search,
                              content_hierarchy_=self.content_hierarchy, date_=analysis_date)
        print('[Success] Textbook Snapshot')
        get_tenant_info(result_loc_=self.data_store_location.joinpath('textbook_reports'), org_search_=self.org_search,
                        date_=analysis_date)
        print('[Success] Tenant Info')
        self.app_and_plays(result_loc_=self.data_store_location.joinpath('play'), date_=analysis_date)
        print('[Success] App and Plays')
        self.dialscans(result_loc_=self.data_store_location.joinpath('dialcode_scans'), date_=analysis_date)
        print('[Success] DIAL Scans')
        self.downloads(result_loc_=self.data_store_location.joinpath('downloads'), date_=analysis_date)
        print('[Success] Downloads')
        self.daily_metrics(read_loc_=self.data_store_location, date_=analysis_date)
        print('[Success] Daily metrics')
        end_time = datetime.now()
        print("Ended at: ", end_time.strftime('%Y-%m-%d %H:%M:%S'))
        print("Time taken: ", str(end_time - start_time))

        end_time_sec = int(round(time.time()))
        time_taken = end_time_sec - start_time_sec
        metrics = [
            {
                "metric": "timeTakenSecs",
                "value": time_taken
            },
            {
                "metric": "date",
                "value": execution_date.strftime("%Y-%m-%d")
            }
        ]
        push_metric_event(metrics, "Consumption Metrics")