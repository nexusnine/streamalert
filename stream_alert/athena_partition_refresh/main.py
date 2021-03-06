"""
Copyright 2017-present, Airbnb Inc.

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

   http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
"""
from __future__ import absolute_import  # Suppresses RuntimeWarning import error in Lambda
from collections import defaultdict
import json
import posixpath
import re
import urllib

from stream_alert.athena_partition_refresh import LOGGER
from stream_alert.shared.athena import AthenaClient
from stream_alert.shared.config import load_config


class AthenaRefreshError(Exception):
    """Generic Athena Partition Error for erroring the Lambda function"""


class AthenaRefresher(object):
    """Handle polling an SQS queue and running Athena queries for updating tables"""

    STREAMALERTS_REGEX = re.compile(r'alerts/dt=(?P<year>\d{4})'
                                    r'\-(?P<month>\d{2})'
                                    r'\-(?P<day>\d{2})'
                                    r'\-(?P<hour>\d{2})'
                                    r'\/.*.json')
    FIREHOSE_REGEX = re.compile(r'(?P<year>\d{4})'
                                r'\/(?P<month>\d{2})'
                                r'\/(?P<day>\d{2})'
                                r'\/(?P<hour>\d{2})\/.*')

    STREAMALERT_DATABASE = '{}_streamalert'
    ATHENA_S3_PREFIX = 'athena_partition_refresh'

    def __init__(self):
        config = load_config(include={'lambda.json', 'global.json'})
        prefix = config['global']['account']['prefix']
        athena_config = config['lambda']['athena_partition_refresh_config']

        self._athena_buckets = athena_config['buckets']

        db_name = athena_config.get(
            'database_name',
            self.STREAMALERT_DATABASE.format(prefix)
        )

        # Get the S3 bucket to store Athena query results
        results_bucket = athena_config.get(
            'results_bucket',
            's3://{}.streamalert.athena-results'.format(prefix)
        )

        self._athena_client = AthenaClient(
            db_name,
            results_bucket,
            self.ATHENA_S3_PREFIX
        )

        self._s3_buckets_and_keys = defaultdict(set)

    def _get_partitions_from_keys(self):
        """Get the partitions that need to be added for the Athena tables

        Returns:
            (dict): representation of tables, partitions and locations to be added
                Example:
                    {
                        'alerts': {
                            '(dt = \'2018-08-01-01\')': 's3://streamalert.alerts/2018/08/01/01'
                        }
                    }
        """
        partitions = defaultdict(dict)

        LOGGER.info('Processing new Hive partitions...')
        for bucket, keys in self._s3_buckets_and_keys.iteritems():
            athena_table = self._athena_buckets.get(bucket)
            if not athena_table:
                # TODO(jacknagz): Add this as a metric
                LOGGER.error('\'%s\' not found in \'buckets\' config. Please add this '
                             'bucket to enable additions of Hive partitions.',
                             bucket)
                continue

            # Iterate over each key
            for key in keys:
                match = None
                for pattern in (self.FIREHOSE_REGEX, self.STREAMALERTS_REGEX):
                    match = pattern.search(key)
                    if match:
                        break

                if not match:
                    LOGGER.error('The key %s does not match any regex, skipping', key)
                    continue

                # Get the path to the objects in S3
                path = posixpath.dirname(key)
                # The config does not need to store all possible tables
                # for enabled log types because this can be inferred from
                # the incoming S3 bucket notification.  Only enabled
                # log types will be sending data to Firehose.
                # This logic extracts out the name of the table from the
                # first element in the S3 path, as that's how log types
                # are configured to send to Firehose.
                if athena_table != 'alerts':
                    athena_table = path.split('/')[0]

                # Example:
                # PARTITION (dt = '2017-01-01-01') LOCATION 's3://bucket/path/'
                partition = '(dt = \'{year}-{month}-{day}-{hour}\')'.format(**match.groupdict())
                location = '\'s3://{bucket}/{path}\''.format(bucket=bucket, path=path)
                # By using the partition as the dict key, this ensures that
                # Athena will not try to add the same partition twice.
                # TODO(jacknagz): Write this dictionary to SSM/DynamoDb
                # to increase idempotence of this Lambda function
                partitions[athena_table][partition] = location

        return partitions

    def _add_partitions(self):
        """Execute a Hive Add Partition command for the given Athena tables and partitions

        Returns:
            (bool): If the repair was successful for not
        """
        partitions = self._get_partitions_from_keys()
        if not partitions:
            LOGGER.error('No partitons to add')
            return False

        for athena_table in partitions:
            partition_statement = ' '.join(
                ['PARTITION {0} LOCATION {1}'.format(partition, location)
                 for partition, location in partitions[athena_table].iteritems()])
            query = ('ALTER TABLE {athena_table} '
                     'ADD IF NOT EXISTS {partition_statement};'.format(
                         athena_table=athena_table,
                         partition_statement=partition_statement))

            success = self._athena_client.run_query(query=query)
            if not success:
                raise AthenaRefreshError(
                    'The add hive partition query has failed:\n{}'.format(query)
                )

            LOGGER.info('Successfully added the following partitions:\n%s',
                        json.dumps({athena_table: partitions[athena_table]}))
        return True

    def run(self, event):
        """Take the messages from the SQS queue and create partitions for new data in S3

        Args:
            event (dict): Lambda input event containing SQS messages. Each SQS message
                should contain one (or maybe more) S3 bucket notification message.
        """
        # Check that the database being used exists before running queries
        if not self._athena_client.check_database_exists():
            raise AthenaRefreshError(
                'The \'{}\' database does not exist'.format(self._athena_client.database)
            )

        for sqs_rec in event['Records']:
            LOGGER.debug('Processing event with message ID \'%s\' and SentTimestamp %s',
                         sqs_rec['messageId'],
                         sqs_rec['attributes']['SentTimestamp'])

            body = json.loads(sqs_rec['body'])
            if body.get('Event') == 's3:TestEvent':
                LOGGER.debug('Skipping S3 bucket notification test event')
                continue

            for s3_rec in body['Records']:
                if 's3' not in s3_rec:
                    LOGGER.info('Skipping non-s3 bucket notification message: %s', s3_rec)
                    continue

                bucket_name = s3_rec['s3']['bucket']['name']

                # Account for special characters in the S3 object key
                # Example: Usage of '=' in the key name
                object_key = urllib.unquote_plus(s3_rec['s3']['object']['key']).decode('utf8')

                LOGGER.debug('Received notification for object \'%s\' in bucket \'%s\'',
                             object_key,
                             bucket_name)

                self._s3_buckets_and_keys[bucket_name].add(object_key)

        if not self._add_partitions():
            raise AthenaRefreshError(
                'Failed to add partitions: {}'.format(dict(self._s3_buckets_and_keys))
            )


def handler(event, _):
    """Athena Partition Refresher Handler Function"""
    AthenaRefresher().run(event)
