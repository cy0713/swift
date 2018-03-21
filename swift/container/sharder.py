# Copyright (c) 2015 OpenStack Foundation
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or
# implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import errno
import json
import os
import time

from random import random

from collections import defaultdict
from eventlet import Timeout

from swift.common.direct_client import (direct_put_container,
                                        DirectClientException)
from swift.container.replicator import ContainerReplicator
from swift.container.backend import ContainerBroker, \
    RECORD_TYPE_SHARD_NODE, UNSHARDED, SHARDING, SHARDED, COLLAPSED
from swift.common import internal_client, db_replicator
from swift.common.exceptions import DeviceUnavailable, RangeAnalyserException
from swift.common.constraints import check_drive, CONTAINER_LISTING_LIMIT
from swift.common.ring.utils import is_local_device
from swift.common.utils import get_logger, config_true_value, \
    dump_recon_cache, whataremyips, Timestamp, ShardRange, GreenAsyncPile, \
    config_float_value, config_positive_int_value, list_from_csv, \
    quorum_size


def sharding_enabled(broker):
    # NB all shards will by default have been created with
    # X-Container-Sysmeta-Sharding set and will therefore be candidates for
    # sharding, along with explicitly configured root containers.
    sharding = broker.metadata.get('X-Container-Sysmeta-Sharding')
    if sharding and config_true_value(sharding[0]):
        return True
    # if broker has been marked deleted it will have lost sysmeta, but we still
    # need to process the broker (for example, to shrink any shard ranges) so
    # fallback to checking if it has any shard ranges
    if broker.get_shard_ranges():
        return True
    return False


def make_shard_ranges(broker, shard_data, shards_account_prefix):
    timestamp = Timestamp.now()
    return [
        ShardRange(ShardRange.make_path(
            shards_account_prefix + broker.root_account,
            broker.root_container, broker.container,
            timestamp, data.pop('index')),
            timestamp, **data)
        for data in shard_data]


class ContainerSharder(ContainerReplicator):
    """Shards containers."""

    def __init__(self, conf, logger=None):
        logger = logger or get_logger(conf, log_route='container-sharder')
        super(ContainerReplicator, self).__init__(conf, logger=logger)
        self.shards_account_prefix = (
            (conf.get('auto_create_account_prefix') or '.') + 'shards_')

        try:
            self.shard_shrink_point = config_float_value(
                conf.get('shard_shrink_point', 25), 0, 100) / 100.0
        except ValueError as err:
            raise ValueError(err.message + ": shard_shrink_point")
        try:
            self.shrink_merge_point = config_float_value(
                conf.get('shard_shrink_merge_point', 75), 0, 100) / 100.0
        except ValueError as err:
            raise ValueError(err.message + ": shard_shrink_merge_point")
        self.shard_container_size = config_positive_int_value(
            conf.get('shard_container_size', 10000000))
        self.shrink_size = self.shard_container_size * self.shard_shrink_point
        self.merge_size = self.shard_container_size * self.shrink_merge_point
        self.split_size = self.shard_container_size // 2
        self.cpool = GreenAsyncPile(self.cpool)
        self.scanner_batch_size = config_positive_int_value(
            conf.get('shard_scanner_batch_size', 10))
        self.shard_batch_size = config_positive_int_value(
            conf.get('shard_batch_size', 2))
        self.reported = None
        self.auto_shard = config_true_value(conf.get('auto_shard', False))

        # internal client
        self.conn_timeout = float(conf.get('conn_timeout', 5))
        request_tries = config_positive_int_value(
            conf.get('request_tries', 3))
        internal_client_conf_path = conf.get('internal_client_conf_path',
                                             '/etc/swift/internal-client.conf')
        try:
            self.swift = internal_client.InternalClient(
                internal_client_conf_path,
                'Swift Container Sharder',
                request_tries,
                allow_modify_pipeline=False)
        except IOError as err:
            # TODO: if sharder functions are moved into replicator then for
            # backwards compatibility we need this to simply log a warning that
            # sharding will be skipped due to missing internal client conf
            if err.errno != errno.ENOENT:
                raise
            raise SystemExit(
                'Unable to load internal client from config: %r (%s)' %
                (internal_client_conf_path, err))

    def _zero_stats(self):
        """Zero out the stats."""
        super(ContainerSharder, self)._zero_stats()
        # all sharding stats that are additional to the inherited replicator
        # stats are maintained under the 'sharding' key in self.stats
        self.stats['sharding'] = defaultdict(lambda: defaultdict(int))

    def _min_stat(self, category, key, value):
        current = self.stats['sharding'][category][key]
        if not current:
            self.stats['sharding'][category][key] = value
        else:
            self.stats['sharding'][category][key] = min(current, value)

    def _max_stat(self, category, key, value):
        current = self.stats['sharding'][category][key]
        if not current:
            self.stats['sharding'][category][key] = value
        else:
            self.stats['sharding'][category][key] = max(current, value)

    def _increment_stat(self, category, key, step=1):
        self.stats['sharding'][category][key] += step

    def _report_stats(self):
        default_stats = ('attempted', 'success', 'failure')
        category_stats = {
            'visited': default_stats + ('skipped',),
            'scanned': default_stats + ('found', 'min_time', 'max_time'),
            'created': default_stats,
            'cleaved': default_stats + ('min_time', 'max_time',),
            'misplaced': default_stats}

        now = time.time()
        last_report = time.ctime(self.reported)
        elapsed = now - self.stats['start']
        sharding_stats = self.stats['sharding']
        for category in (
                'visited', 'scanned', 'created', 'cleaved', 'misplaced'):
            stats = sharding_stats[category]
            msg = ', '.join(['%s %s' % (str(stats[k]), k)
                             for k in category_stats[category]])
            self.logger.info(
                'Since %s: %s: %s', last_report, category, msg)

        dump_recon_cache(
            {'sharding_stats': self.stats,
             'sharding_time': elapsed,
             'sharding_last': now},
            self.rcache, self.logger)
        self.reported = now

    def _periodic_report_stats(self):
        if (time.time() - self.reported) >= 3600:  # once an hour
            self._report_stats()
            self._zero_stats()

    def _fetch_shard_ranges(self, broker, newest=False):
        path = self.swift.make_path(broker.root_account, broker.root_container)
        path += '?format=json'
        headers = {'X-Backend-Record-Type': 'shard'}
        if newest:
            headers['X-Newest'] = 'true'
        try:
            resp = self.swift.make_request('GET', path, headers,
                                           acceptable_statuses=(2,))
        except internal_client.UnexpectedResponse:
            self.logger.error("Failed to get shard ranges from %s",
                              broker.root_path)
            return None

        return [ShardRange.from_dict(shard_range)
                for shard_range in json.loads(resp.body)]

    def _put_container(self, node, part, account, container, headers, body):
        try:
            direct_put_container(node, part, account, container,
                                 conn_timeout=self.conn_timeout,
                                 response_timeout=self.node_timeout,
                                 headers=headers, contents=body)
        except DirectClientException as err:
            self.logger.warning(
                'Failed to put shard ranges to %s:%s/%s: %s',
                node['ip'], node['port'], node['device'], err.http_status)
        except Exception as err:
            self.logger.exception(
                'Failed to put shard ranges to %s:%s/%s: %s',
                node['ip'], node['port'], node['device'], err)
        else:
            return True
        return False

    def _send_shard_ranges(self, account, container, shard_ranges,
                           headers=None):
        body = json.dumps([dict(sr) for sr in shard_ranges])
        part, nodes = self.ring.get_nodes(account, container)
        headers = headers or {}
        headers.update({'X-Backend-Record-Type': RECORD_TYPE_SHARD_NODE,
                        'User-Agent': 'container-sharder %s' % os.getpid(),
                        'X-Timestamp': Timestamp.now().normal,
                        'Content-Length': len(body),
                        'Content-Type': 'application/json'})

        for node in nodes:
            self.cpool.spawn(self._put_container, node, part, account,
                             container, headers, body)

        results = self.cpool.waitall(None)
        return results.count(True) >= quorum_size(self.ring.replica_count)

    def _get_shard_broker(self, shard_range, root_path, policy_index,
                          force=False):
        """
        Get a container broker for given shard range.

        :param shard_range: a :class:`~swift.common.utils.ShardRange`
        :param root_path: the path of the shard's root container
        :param policy_index: the storage policy index
        :param force: if True set the broker's sharding metadata even if
            sharding metadata already exists
        :returns: a shard container broker
        """
        part = self.ring.get_part(shard_range.account, shard_range.container)
        node = self.find_local_handoff_for_part(part)
        if not node:
            # TODO: and when *do* we cleave? maybe we should just be picking
            # one of the local devs
            raise DeviceUnavailable(
                'No mounted devices found suitable to Handoff sharded '
                'container %s in partition %s' % (shard_range.name, part))

        shard_broker = self._initialize_broker(
            node['device'], part, shard_range.account, shard_range.container,
            storage_policy_index=policy_index)

        # Get the valid info into the broker.container, etc
        shard_broker.get_info()

        if not shard_broker.get_sharding_info('Root') or force:
            shard_broker.merge_shard_ranges([shard_range])
            shard_broker.update_sharding_info({'Root': root_path})
            shard_broker.update_metadata({
                'X-Container-Sysmeta-Sharding':
                    ('True', Timestamp.now().internal)})

        return part, shard_broker, node['id']

    def yield_objects(self, broker, src_shard_range, policy_index):
        """
        Iterates through all objects in ``src_shard_range`` in name order
        yielding them in lists of up to CONTAINER_LISTING_LIMIT length.

        :param broker: A :class:`~swift.container.backend.ContainerBroker`.
        :param src_shard_range: A :class:`~swift.common.utils.ShardRange`
            describing the source range.
        :param policy_index: The storage policy index.
        :return: lists objects
        """
        # Since we're going to change lower attribute in the loop...
        src_shard_range = src_shard_range.copy()

        # TODO: list_objects_iter transforms the timestamp, losing info
        # that we want to copy - see _transform_record - we need to
        # override that behaviour

        while True:
            objects = broker.get_objects(
                CONTAINER_LISTING_LIMIT,
                marker=str(src_shard_range.lower),
                end_marker=src_shard_range.end_marker,
                storage_policy_index=policy_index,
                include_deleted=True)
            yield objects

            if len(objects) < CONTAINER_LISTING_LIMIT:
                break
            src_shard_range.lower = objects[-1]['name']

    def yield_objects_to_shard_range(self, broker, src_shard_range,
                                     policy_index, dest_shard_ranges):
        """
        Iterates through all objects in ``src_shard_range`` to place them in
        destination shard ranges provided by the ``next_shard_range`` function.
        Yields tuples of (object list, destination shard range in which those
        objects belong). Note that the same destination shard range may be
        referenced in more than one yielded tuple.

        :param broker: A :class:`~swift.container.backend.ContainerBroker`.
        :param src_shard_range: A :class:`~swift.common.utils.ShardRange`
            describing the source range.
        :param policy_index: The storage policy index.
        :param dest_shard_ranges: A function which should return a list of
            destination shard ranges in name order.
        :return: tuples of (object list, shard range)
        """
        dest_shard_range_iter = dest_shard_range = None
        for objs in self.yield_objects(broker, src_shard_range, policy_index):
            if not objs:
                return

            def next_or_none(it):
                try:
                    return next(it)
                except StopIteration:
                    return None

            if dest_shard_range_iter is None:
                dest_shard_range_iter = iter(dest_shard_ranges())
                dest_shard_range = next_or_none(dest_shard_range_iter)

            unplaced = False
            last_index = next_index = 0
            for obj in objs:
                if dest_shard_range is None:
                    # no more destinations: yield remainder of batch and return
                    # NB there may be more batches of objects but none of them
                    # will be placed so no point fetching them
                    yield objs[last_index:], None
                    return
                if obj['name'] <= dest_shard_range.lower:
                    unplaced = True
                elif unplaced:
                    # end of run of unplaced objects, yield them
                    yield objs[last_index:next_index], None
                    last_index = next_index
                    unplaced = False
                while (dest_shard_range and
                       obj['name'] > dest_shard_range.upper):
                    if next_index != last_index:
                        # yield the objects in current dest_shard_range
                        yield objs[last_index:next_index], dest_shard_range
                    last_index = next_index
                    dest_shard_range = next_or_none(dest_shard_range_iter)
                next_index += 1

            if next_index != last_index:
                # yield tail of current batch of objects
                # NB there may be more objects for the current
                # dest_shard_range in the next batch from yield_objects
                yield (objs[last_index:next_index],
                       None if unplaced else dest_shard_range)

    def _move_misplaced_objects(self, broker, src_shard_range, policy_index,
                                dest_shard_ranges):
        # map shard range -> broker
        brokers = {}
        placed = unplaced = 0
        for objs, dest_shard_range in self.yield_objects_to_shard_range(
                broker, src_shard_range, policy_index, dest_shard_ranges):
            if not dest_shard_range:
                unplaced += len(objs)
                continue

            if (dest_shard_range.name ==
                    '%s/%s' % (broker.account, broker.container)):
                # in shrinking context, the misplaced objects might actually be
                # correctly placed if the root has expanded this shard but this
                # broker has not yet been updated
                continue

            if dest_shard_range not in brokers:
                part, dest_broker, node_id = self._get_shard_broker(
                    dest_shard_range, broker.root_path, policy_index)
                brokers[dest_shard_range] = (part, dest_broker, node_id)
            else:
                part, dest_broker, node_id = brokers[dest_shard_range]
            dest_broker.merge_items(objs)
            placed += len(objs)

        if unplaced:
            self.logger.warning(
                'Failed to find destination for at least %s misplaced objects'
                % unplaced)

        if not brokers:
            return False

        for dest_shard_range, (part, dest_broker, node_id) in brokers.items():
            self.logger.debug('moving misplaced objects found in range %s' %
                              dest_shard_range)
            self.cpool.spawn(
                self._replicate_object, part, dest_broker.db_file, node_id)

        self.cpool.waitall(None)
        # TODO: we need to be confident that replication succeeded before
        # removing misplaced items from source - if not then just leave the
        # misplaced items in source, but do not mark them as deleted, and
        # leave for next cycle to try again
        for dest_shard_range in brokers:
            broker.remove_objects(
                str(dest_shard_range.lower), str(dest_shard_range.upper),
                policy_index)

        self.logger.debug('Moved %s misplaced objects' % placed)
        return True

    def _misplaced_objects(self, broker, own_shard_range):
        """
        Search for objects in the given broker that do not belong in that
        broker's namespace and move those objects to their correct shard
        container.

        :param broker: An instance of :class:`swift.container.ContainerBroker`
        :param own_shard_range: A ShardRange describing the namespace for this
            broker
        """

        if broker.is_deleted():
            self.logger.debug('Not looking for misplaced objects in deleted '
                              'container %s (%s)', broker.path, broker.db_file)
            return
        if own_shard_range.state == ShardRange.EXPANDING:
            self.logger.debug('Not looking for misplaced objects in expanding '
                              'container %s (%s)', broker.path, broker.db_file)
            return

        self.logger.debug('Looking for misplaced objects in %s (%s)',
                          broker.path, broker.db_file)
        self._increment_stat('misplaced', 'attempted')

        def make_query(lower, upper):
            # each misplaced object namespace is represented by a shard range
            return ShardRange('dont/care', Timestamp.now(), lower, upper)

        queries = []
        policy_index = broker.storage_policy_index
        # TODO: what about records for objects in the wrong storage policy?

        state = broker.get_db_state()
        if state == SHARDED:
            # Anything in the object table is treated as a misplaced object.
            queries.append(make_query('', ''))

        if not queries and state == SHARDING:
            # Objects outside of this container's own range are misplaced.
            # Objects in already cleaved shard ranges are also misplaced.
            # TODO: if we redirect new udpates to CREATED shards then should we
            # also treat objects in CREATED shards as misplaced, as well as
            # ACTIVE shards?
            cleave_context = broker.load_cleave_context()
            cleave_cursor = cleave_context.get('cursor')
            if cleave_cursor:
                queries.append(make_query('', cleave_cursor))
                if own_shard_range.upper:
                    queries.append(make_query(own_shard_range.upper, ''))

        if not queries:
            # Objects outside of this container's own range are misplaced.
            if own_shard_range.lower:
                queries.append(make_query('', own_shard_range.lower))
            if own_shard_range.upper:
                queries.append(make_query(own_shard_range.upper, ''))

        if not queries:
            return

        def make_dest_shard_ranges():
            # returns a function that will lazy load shard ranges on demand;
            # this means only one lookup is made for all misplaced ranges.
            outer = {}

            def dest_shard_ranges():
                if not outer:
                    if broker.is_root_container():
                        ranges = broker.get_shard_ranges()
                    else:
                        # TODO: the root may not yet know about shard ranges to
                        # which a shard is sharding - those need to come from
                        # the broker
                        ranges = self._fetch_shard_ranges(broker, newest=True)
                    outer['ranges'] = iter(ranges)
                return outer['ranges']
            return dest_shard_ranges

        self.logger.debug('misplaced object queries %s' % queries)
        misplaced_items = False
        for query in queries:
            misplaced_items |= self._move_misplaced_objects(
                broker, query, policy_index, make_dest_shard_ranges())

        if misplaced_items:
            self.logger.increment('misplaced_items_found')
            self._increment_stat('misplaced', 'success')

        # wipe out the cache to disable bypass in delete_db
        cleanups = self.shard_cleanups or {}
        self.shard_cleanups = None
        self.logger.info('Cleaning up %d replicated shard containers',
                         len(cleanups))
        for container in cleanups.values():
            self.cpool.spawn(self.delete_db, container)
        self.cpool.waitall(None)
        self.logger.info('Finished misplaced shard replication')

    def _post_replicate_hook(self, broker, info, responses):
        return

    def delete_db(self, broker):
        """
        Ensure that replicated sharded databases are only cleaned up at the end
        of the replication run.
        """
        if self.shard_cleanups is not None:
            # this container shouldn't be here, make sure it's cleaned up
            self.shard_cleanups[broker.container] = broker
            return
        return super(ContainerReplicator, self).delete_db(broker)

    def _audit_shard_container(self, broker, shard_range):
        # TODO We will need to audit the root (make sure there are no missing
        #      gaps in the ranges.
        # TODO If the shard container has a sharding lock then see if it's
        #       needed. Maybe something as simple as sharding lock older then
        #       reclaim age.

        self.logger.info('Auditing %s/%s', broker.account, broker.container)
        continue_with_container = True

        # if the container has been marked as deleted, all metadata will
        # have been erased so no point auditing. But we want it to pass, in
        # case any objects exist inside it.
        if broker.is_deleted():
            return continue_with_container

        if broker.is_root_container():
            # This is the root container, and therefore the tome of knowledge,
            # all we can do is check there is nothing screwy with the range
            shard_ranges = broker.get_shard_ranges()
            overlaps = ContainerSharder.find_overlapping_ranges(shard_ranges)
            for overlap in overlaps:
                self.logger.error('Range overlaps found, attempting to '
                                  'correct')
                newest = max(overlap, key=lambda x: x.timestamp)
                older = set(overlap).difference(set([newest]))

                # now delete the older overlaps, keeping only the newest
                timestamp = Timestamp(newest.timestamp, offset=1)
                for shard_range in older:
                    shard_range.timestamp = timestamp
                    shard_range.deleted = 1
                # TODO: make a single update of older ranges at end of loop
                self._send_shard_ranges(
                    broker.root_account, broker.root_container, older)
                continue_with_container = False
            missing_ranges = ContainerSharder.check_complete_ranges(
                shard_ranges)
            if missing_ranges:
                self.logger.error('Missing range(s) dectected: %s',
                                  '-'.join(missing_ranges))
                continue_with_container = False

            if not continue_with_container:
                self.logger.increment('audit_failed')
                self._increment_stat('audit', 'failure')
            return continue_with_container

        # Get the root view of the world.
        shard_ranges = self._fetch_shard_ranges(broker, newest=True)
        if shard_ranges is None:
            # failed to get the root tree. Error out for now.. we may need to
            # quarantine the container.
            self.logger.warning("Failed to get a shard range tree from root "
                                "container %s, it may not exist.",
                                broker.root_path)
            self.logger.increment('audit_failed')
            self._increment_stat('audit', 'failure')
            return False
        if shard_range in shard_ranges:
            return continue_with_container

        # shard range isn't in ranges, if it overlaps with an item, we're in
        # trouble. If there is overlap let's see if it's newer than this
        # container, if so, it's safe to delete (quarantine this container).
        # TODO(tburke): is ^^^ right? or better to consider it all misplaced?
        # if it's newer, then it might not be updated yet, so just let it
        # continue (or maybe we shouldn't?).
        overlaps = [r for r in shard_ranges if r.overlaps(shard_range)]
        if overlaps:
            if max(overlaps + [shard_range],
                   key=lambda x: x.timestamp) == shard_range:
                # shard range is newest so leave it alone for now  as the root
                # might not be updated  yet.
                self.logger.increment('audit_failed')
                self._increment_stat('audit', 'failure')
                return False
            else:
                # There is a newer range that overlaps/covers this range.
                # so we are safe to quarantine it.
                # TODO Quarantine
                self.logger.error('The range of objects stored in this '
                                  'container (%s/%s) overlaps with another '
                                  'newer shard range',
                                  broker.account, broker.container)
                self.logger.increment('audit_failed')
                self._increment_stat('audit', 'failure')
                return False
        # shard range doesn't exist in the root containers ranges, but doesn't
        # overlap with anything
        return continue_with_container

    def _process_broker(self, broker, node, part):
        own_shard_range = broker.get_own_shard_range()
        # TODO: sigh, we should get the info cached *once*, somehow
        broker.get_info()  # make sure account/container are populated
        state = broker.get_db_state()
        self.logger.info('Starting processing %s/%s state %s',
                         broker.account, broker.container,
                         broker.get_db_state_text(state))

        # Before we do any heavy lifting, lets do an audit on the shard
        # container. We grab the root's view of the shard_points and make
        # sure this container exists in it and in what should be it's
        # parent. If its in both great, If it exists in either but not the
        # other, then this needs to be fixed. If, however, it doesn't
        # exist in either then this container may not exist anymore so
        # quarantine it.
        # if not self._audit_shard_container(broker, shard_range):
        #     continue

        # now look and deal with misplaced objects.
        self._misplaced_objects(broker, own_shard_range)

        if broker.is_deleted():
            # This container is deleted so we can skip it. We still want
            # deleted containers to go via misplaced items because they may
            # have new objects sitting in them that may need to move.
            return

        self.shard_cleanups = dict()
        # TODO: bring back leader election (maybe?); if so make it
        # on-demand since we may not need to know if we are leader for all
        # states
        is_leader = node['index'] == 0 and self.auto_shard
        try:
            if state in (UNSHARDED, COLLAPSED):
                if is_leader and broker.is_root_container():
                    # bootstrap sharding of root container
                    if self._find_sharding_candidates(
                            broker, shard_ranges=[own_shard_range]):
                        own_shard_range = broker.get_own_shard_range()

                if broker.get_shard_ranges():
                    # container may have been given shard ranges rather
                    # than found them e.g. via replication or a shrink event
                    # TODO: broker *should* also have had a sharding epoch set
                    # but should we double check? If so, only leader should set
                    # the epoch.
                    broker.set_sharding_state()
                    state = SHARDING
                elif own_shard_range.state in (ShardRange.SHARDING,
                                               ShardRange.SHRINKING):
                    broker.update_sharding_info({'Scan-Done': 'False'})
                    own_shard_range.update_state(ShardRange.SHARDING)
                    broker.set_sharding_state(epoch=Timestamp.now())
                    state = SHARDING

            if state == SHARDING:
                num_found = num_created = 0
                scan_complete = config_true_value(
                    broker.get_sharding_info('Scan-Done'))
                if is_leader and not scan_complete:
                    scan_complete, num_found = self._find_shard_ranges(broker)

                if is_leader:
                    # create shard containers for newly found ranges
                    # TODO: the current messing with timestamps when creating
                    # the shard container makes it safer to restrict this to
                    # the leader; an improved container creation strategy might
                    # allow this to run on any node
                    num_created = self._create_shard_containers(broker)

                # TODO: what if this replication attempt fails? if we wrote the
                # db then replicator should eventually find it, but what if we
                # failed to create the handoff db? Perhaps there should be some
                # state propagated via cleaving to the shard container and then
                # included in shard range update back to root so we know when
                # cleaving succeeded?
                if num_found or num_created:
                    # share the shard ranges with other nodes so they can start
                    # cleaving
                    self.cpool.spawn(
                        self._replicate_object, part, broker.db_file,
                        node['id'])
                    self.cpool.waitall(None)

                # always try to cleave any pending shard ranges
                cleave_complete = self._cleave(broker)

                if scan_complete and cleave_complete:
                    # This container has been completely cleaved. Move all
                    # CLEAVED shards to ACTIVE state and delete own shard
                    # range; these changes will be simultaneously reported in
                    # the next update to the root container.
                    self.logger.info('Completed cleaving of %s/%s',
                                     broker.account, broker.container)
                    modified_shard_ranges = broker.get_shard_ranges(
                        states=ShardRange.CLEAVED)
                    for sr in modified_shard_ranges:
                        sr.update_state(ShardRange.ACTIVE)
                    own_shard_range = broker.get_own_shard_range()
                    own_shard_range.update_state(ShardRange.SHARDED)
                    own_shard_range.update_meta(0, 0)
                    if (not broker.is_root_container() and not
                            own_shard_range.deleted):
                        own_shard_range = own_shard_range.copy(
                            timestamp=Timestamp.now(), deleted=1)
                    modified_shard_ranges.append(own_shard_range)
                    broker.merge_shard_ranges(modified_shard_ranges)

                    if broker.set_sharded_state():
                        state = SHARDED
                        self.logger.increment('sharding_complete')
                    else:
                        self.logger.debug('Remaining in sharding state %s/%s',
                                          broker.account, broker.container)

            if state == SHARDED and broker.is_root_container():
                if is_leader:
                    self._find_shrinks(broker)
                    self._find_sharding_candidates(broker)
                for shard_range in broker.get_shard_ranges(
                        states=[ShardRange.SHARDING]):
                    self._send_shard_ranges(
                        shard_range.account, shard_range.container,
                        [shard_range])

            if not broker.is_root_container():
                # Update the root container with this container's shard range
                # info; do this even when sharded in case previous attempts
                # failed; don't do this if there is no shard range info. When
                # sharding a shard, this is when the root will see the new
                # shards move to ACTIVE state and the sharded shard
                # simultaneously become deleted.
                # TODO: the new shard ranges may have existed for some
                # time and already be updating the root with their
                # usage e.g. multiple cycles before we finished
                # cleaving, or process failed right here. If so then
                # the updates we send here probably have an out of date
                # view of the shards' usage, but I think it is still ok
                # to send these updates because the meta_timestamp
                # should prevent these updates undoing any newer
                # updates at the root from the actual shards. *It would
                # be good to have a test to verify that.*
                own_shard_range = broker.get_own_shard_range(no_default=True)
                if own_shard_range:
                    # persist the reported shard metadata
                    broker.merge_shard_ranges([own_shard_range])
                    shrinking = own_shard_range.state == ShardRange.SHRINKING
                    self._send_shard_ranges(
                        broker.root_account, broker.root_container,
                        broker. get_shard_ranges(
                            include_own=True,
                            include_deleted=True,
                            exclude_others=shrinking)
                    )
            self.logger.info('Finished processing %s/%s state %s',
                             broker.account, broker.container,
                             broker.get_db_state_text())
        finally:
            self.logger.increment('scanned')

    def _check_node(self, node, override_devices):
        if not node:
            return False
        if override_devices and node['device'] not in override_devices:
            return False
        if not is_local_device(self.ips, self.port,
                               node['replication_ip'],
                               node['replication_port']):
            return False
        if not check_drive(self.root, node['device'],
                           self.mount_check):
            self.logger.warning(
                'Skipping %(device)s as it is not mounted' % node)
            return False
        return True

    def _one_shard_cycle(self, override_devices=None,
                         override_partitions=None):
        """
        The main function, everything the sharder does forks from this method.

        The sharder loops through each container with sharding enabled and each
        sharded container on the server, on each container it:
            - audits the container
            - checks and deals with misplaced items
            - 2 phase sharding (if no. objects > shard_container_size)
                - Phase 1, if there is no shard defined, find it, then move
                  to next container.
                - Phase 2, if there is a shard defined, shard it.
            - Shrinking (check to see if we need to shrink this container).
        """
        self.logger.info('Container sharder cycle starting, auto-sharding %s',
                         self.auto_shard)
        self._zero_stats()
        if override_devices:
            self.logger.info('(Override devices: %s)',
                             ', '.join(override_devices))
        if override_partitions:
            self.logger.info('(Override partitions: %s)',
                             ', '.join(override_partitions))
        dirs = []
        self.shard_cleanups = dict()
        self.ips = whataremyips(bind_ip=self.bind_ip)
        for node in self.ring.devs:
            if not self._check_node(node, override_devices):
                continue
            datadir = os.path.join(self.root, node['device'], self.datadir)
            if os.path.isdir(datadir):
                # Populate self._local_device_ids so we can find
                # handoffs for shards later
                self._local_device_ids.add(node['id'])
                dirs.append((datadir, node['id']))
        if not dirs:
            self.logger.warning('Found no data dirs!')
        for part, path, node_id in db_replicator.roundrobin_datadirs(dirs):
            # NB: get_part_nodes always provides an 'index' key
            if override_partitions and part not in override_partitions:
                continue
            for node in self.ring.get_part_nodes(int(part)):
                if node['id'] == node_id:
                    break
            else:
                # TODO: this would be a bug, a warning log may be too soft
                # tburke: or is it just a handoff? doesn't seem exceptional...
                self.logger.warning("Failed to find node to match id %s" %
                                    node_id)
                continue

            broker = ContainerBroker(path, logger=self.logger)
            if sharding_enabled(broker):
                try:
                    self._increment_stat('visited', 'attempted')
                    self._process_broker(broker, node, part)
                except Exception as err:
                    self._increment_stat('visited', 'failure')
                    self.logger.exception(
                        'Unhandled exception while processing %s: %s',
                        path, err)
                else:
                    self._increment_stat('visited', 'success')
            else:
                self._increment_stat('visited', 'skipped')
                self.logger.info('Skipping %s; sharding is not enabled', path)

            self._periodic_report_stats()

        # wipe out the cache do disable bypass in delete_db
        cleanups = self.shard_cleanups
        self.shard_cleanups = None
        if cleanups:
            self.logger.info('Cleaning up %d replicated shard containers',
                             len(cleanups))

            for container in cleanups.values():
                self.cpool.spawn(self.delete_db, container)

        # Now we wait for all threads to finish.
        self.cpool.waitall(None)
        self._report_stats()

    @staticmethod
    def check_complete_ranges(ranges):
        lower = set()
        upper = set()
        for r in ranges:
            lower.add(r.lower)
            upper.add(r.upper)
        l = lower.copy()
        lower.difference_update(upper)
        upper.difference_update(l)

        return zip(upper, lower)

    @staticmethod
    def find_overlapping_ranges(ranges):
        result = set()
        for range in ranges:
            res = [r for r in ranges if range != r and range.overlaps(r)]
            if res:
                res.append(range)
                res.sort()
                result.add(tuple(res))

        return result

    def _find_shard_ranges(self, broker):
        """
        This function is the main work horse of a scanner node, it:
          - Looks at the shard_ranges table to see where to continue on from.
          - Once it finds the next shard_range it'll ask for a quorum as to
             whether this node is still in fact a scanner node.
          - If it is still the scanner, it's time to add it to the shard_ranges
            table.

        :param broker:
        :return: True if the last shard range was found, False otherwise
        """
        self.logger.info('Started scan for shard ranges on %s/%s',
                         broker.account, broker.container)
        self._increment_stat('scanned', 'attempted')

        start = time.time()
        shard_data, last_found = broker.find_shard_ranges(
            self.shard_container_size // 2, limit=self.scanner_batch_size,
            existing_ranges=broker.get_shard_ranges())
        elapsed = time.time() - start

        if not shard_data:
            if last_found:
                self.logger.info("Already found all shard ranges")
                # set scan done in case it's missing
                broker.update_sharding_info({'Scan-Done': 'True'})
            else:
                # we didn't find anything
                self._increment_stat('scanned', 'failure')
                self.logger.warning("No shard ranges found, something went "
                                    "wrong. We will try again next cycle.")
            return last_found, 0

        # TODO: if we bring back leader election, this is about the spot where
        # we should confirm we're still the scanner

        shard_ranges = make_shard_ranges(
            broker, shard_data, self.shards_account_prefix, )
        broker.merge_shard_ranges(shard_ranges)
        if not broker.is_root_container():
            # TODO: check for success and do not proceed otherwise
            self._send_shard_ranges(
                broker.root_account, broker.root_container, shard_ranges)

        num_found = len(shard_ranges)
        self.logger.info(
            "Completed scan for shard ranges: %d found", num_found)
        self._increment_stat('scanned', 'found', step=num_found)
        self._min_stat('scanned', 'min_time', round(elapsed / num_found, 3))
        self._max_stat('scanned', 'max_time', round(elapsed / num_found, 3))

        if last_found:
            # We've found the last shard range, so mark that in metadata
            broker.update_sharding_info({'Scan-Done': 'True'})
            self.logger.info("Final shard range reached.")
        self._increment_stat('scanned', 'success')
        return last_found, num_found

    def _create_shard_containers(self, broker):
        # Create shard containers that are ready to receive redirected object
        # updates. Do this now, so that redirection can begin immediately
        # without waiting for cleaving to complete.
        found_ranges = broker.get_shard_ranges(states=ShardRange.FOUND)
        created_ranges = []
        for shard_range in found_ranges:
            self._increment_stat('created', 'attempted')
            # Create newly found shard containers with a timestamp that is
            # slightly older that the persisted shard range. Why? The new shard
            # container will initially be empty. As a result, any shard range
            # updates received at the root from the shard container are likely
            # to make the new shard range mistakenly appear ready for
            # shrinking. Creating the shard container with an older timestamp
            # means that updates from it will be ignored until cleaving
            # replicates objects to it and simultaneously updates timestamps to
            # the persisted shard range timestamps.
            # TODO: avoid these timestamp shenanigans by sending state to the
            # shard container and have it not send updates until in active
            # state
            older_shard_range = shard_range.copy(
                timestamp=Timestamp(float(shard_range.timestamp) - 1))
            headers = {
                'X-Backend-Storage-Policy-Index': broker.storage_policy_index,
                'X-Container-Sysmeta-Shard-Root': broker.root_path,
                'X-Container-Sysmeta-Sharding': True}
            success = self._send_shard_ranges(
                shard_range.account, shard_range.container,
                [older_shard_range], headers=headers)
            if success:
                self.logger.debug('PUT new shard range container for %s',
                                  older_shard_range)
                self._increment_stat('created', 'success')
            else:
                self.logger.error(
                    'PUT of new shard container %r failed for %s.',
                    older_shard_range, broker.path)
                self._increment_stat('created', 'failure')
                # break, not continue, because elsewhere it is assumed that
                # finding and cleaving shard ranges progresses linearly, so we
                # do not want any subsequent shard ranges to be in created
                # state while this one is still in found state
                break
            shard_range.update_state(ShardRange.CREATED)
            created_ranges.append(shard_range)
            self.logger.increment('shard_ranges_created')

        broker.merge_shard_ranges(created_ranges)
        if not broker.is_root_container():
            # TODO: check for success and do not proceed otherwise
            self._send_shard_ranges(
                broker.root_account, broker.root_container, created_ranges)
        self.logger.info(
            "Completed creating shard range containers: %d created.",
            len(created_ranges))
        return len(created_ranges)

    def _find_sharding_candidates(self, broker, shard_ranges=None):
        # this should only execute on root containers; the goal is to find
        # large shard containers that should be sharded.
        # First cut is simple: assume root container shard usage stats are good
        # enough to make decision.
        # TODO: object counts may well not be the appropriate metric for
        # deciding to shrink because a shard with low object_count may have a
        # large number of deleted object rows that will need to be merged with
        # a neighbour. We may need to expose row count as well as object count.
        if shard_ranges is None:
            shard_ranges = broker.get_shard_ranges(states=[ShardRange.ACTIVE])
        candidates = []
        for shard_range in shard_ranges:
            if shard_range.object_count >= self.shard_container_size:
                shard_range.update_state(ShardRange.SHARDING)
                candidates.append(shard_range)
        broker.merge_shard_ranges(candidates)
        return len(candidates)

    def _find_shrinks(self, broker):
        # this should only execute on root containers that have sharded; the
        # goal is to find small shard containers that could be retired by
        # merging with a neighbour.
        # First cut is simple: assume root container shard usage stats are good
        # enough to make decision; only merge with upper neighbour so that
        # upper bounds never change (shard names include upper bound).
        # TODO: object counts may well not be the appropriate metric for
        # deciding to shrink because a shard with low object_count may have a
        # large number of deleted object rows that will need to be merged with
        # a neighbour. We may need to expose row count as well as object count.
        state = broker.get_db_state()
        if state != SHARDED:
            self.logger.warning(
                'Cannot shrink a not yet sharded container %s/%s',
                broker.account, broker.container)

        shard_ranges = broker.get_shard_ranges()
        own_shard_range = broker.get_own_shard_range()
        if len(shard_ranges) == 1:
            # special case to enable final shard to shrink into root
            shard_ranges.append(own_shard_range)

        merge_pairs = {}
        for donor, acceptor in zip(shard_ranges, shard_ranges[1:]):
            if donor in merge_pairs:
                # this range may already have been made an acceptor; if so then
                # move on. In principle it might be that even after expansion
                # this range and its donor(s) could all be merged with the next
                # range. In practice it is much easier to reason about a single
                # donor merging into a single acceptor. Don't fret - eventually
                # all the small ranges will be retired.
                continue
            if (acceptor is not own_shard_range and acceptor.state not in
                    (ShardRange.ACTIVE, ShardRange.EXPANDING)):
                # don't shrink into a range that is not yet ACTIVE or was
                # selected as a donor on a previous cycle
                continue
            if donor.state not in (ShardRange.ACTIVE, ShardRange.SHRINKING):
                # found? created? sharded? don't touch it
                continue

            proposed_object_count = donor.object_count + acceptor.object_count
            if (donor.state == ShardRange.SHRINKING or
                    (donor.object_count < self.shrink_size and
                     proposed_object_count < self.merge_size)):
                # include previously identified merge pairs on presumption that
                # following shrink procedure is idempotent
                merge_pairs[acceptor] = donor

        # TODO: think long and hard about the correct order for these remaining
        # operations and what happens when one fails...
        for acceptor, donor in merge_pairs.items():
            # TODO: unit test to verify idempotent nature of this procedure
            self.logger.debug('shrinking shard range %s into %s in %s' %
                              (donor, acceptor, broker.db_file))
            modified_shard_ranges = []
            if acceptor.update_state(ShardRange.EXPANDING):
                modified_shard_ranges.append(acceptor)
            if donor.update_state(ShardRange.SHRINKING):
                # Set donor state to shrinking so that next cycle won't use it
                # as an acceptor; state_timestamp defines new epoch for donor
                # and new timestamp for the expanded acceptor below.
                modified_shard_ranges.append(donor)
            broker.merge_shard_ranges(modified_shard_ranges)
            if acceptor is not own_shard_range:
                # Update the acceptor container with its expanding state to
                # prevent it treating objects cleaved from the donor
                # as misplaced.
                self._send_shard_ranges(
                    acceptor.account, acceptor.container, [acceptor])
                # Make a copy the acceptor shard range to send to the donor
                # container with new timestamp and expanded namespace. Note
                # that the new acceptor namespace is not yet saved in the root.
                acceptor = acceptor.copy(timestamp=donor.state_timestamp,
                                         lower=donor.lower,
                                         state=ShardRange.ACTIVE)
                acceptor.object_count += donor.object_count
                acceptor.bytes_used += donor.bytes_used
            else:
                # no need to change namespace or stats
                acceptor.update_state(ShardRange.ACTIVE)
            # Set Scan-Done so that the donor will not scan itself and will
            # transition to SHARDED state once it has cleaved to the acceptor;
            # TODO: if the PUT request successfully write the Scan-Done sysmeta
            # but fails to write the acceptor shard range, then the shard is
            # left with Scan-Done set, and if it was to then grow in size to
            # eventually need sharding, we don't want Scan-Done prematurely
            # set. This is an argument for basing 'scan done condition' on the
            # existence of a shard range whose upper >= shard own range, rather
            # than using sysmeta Scan-Done.
            headers = {
                'X-Container-Sysmeta-Shard-Scan-Done': True,
                'X-Container-Sysmeta-Shard-Epoch': donor.state_timestamp.normal
            }
            # Now send a copy of the expanded acceptor, with an updated
            # timestamp, to the donor container. This forces the donor to
            # asynchronously cleave its entire contents to the acceptor. The
            # donor will pass a deleted copy of its own shard range and the
            # newer expended acceptor shard range to the acceptor when
            # cleaving. Subsequent updates from the acceptor will then update
            # the root to have the expanded acceptor namespace and deleted
            # donor shard range. Once cleaved, the donor will also update the
            # root directly with its deleted own shard range and the expanded
            # acceptor shard range.
            self._send_shard_ranges(donor.account, donor.container,
                                    [donor, acceptor], headers=headers)

    def _cleave(self, broker):
        # Returns True if all available shard ranges have been successfully
        # cleaved, False otherwise
        state = broker.get_db_state()
        if state == SHARDED:
            self.logger.debug('Passing over already sharded container %s/%s',
                              broker.account, broker.container)
            return True

        cleave_context = broker.load_cleave_context()
        if cleave_context.get('done', False):
            self.logger.debug('Passing over already cleaved container %s/%s',
                              broker.account, broker.container)
            return True

        cleave_cursor = cleave_context.get('cursor') or ''
        ranges_todo = broker.get_shard_ranges(
            marker=cleave_cursor + '\x00',
            states=[ShardRange.CREATED, ShardRange.CLEAVED, ShardRange.ACTIVE])
        if not ranges_todo:
            self.logger.debug('No uncleaved shard ranges for %s/%s',
                              broker.account, broker.container)
            return True

        self.logger.debug('%s to cleave %s/%s',
                          'Continuing' if cleave_cursor else 'Starting',
                          broker.account, broker.container)

        own_shard_range = broker.get_own_shard_range()
        ranges_done = []
        policy_index = broker.storage_policy_index
        brokers = broker.get_brokers()
        for shard_range in ranges_todo[:self.shard_batch_size]:
            self.logger.info(
                "Cleaving '%s/%s': %r",
                broker.account, broker.container, shard_range)
            self._increment_stat('cleaved', 'attempted')
            start = time.time()
            shrinking = shard_range.includes(own_shard_range)
            try:
                # use force here because we may want to update existing shard
                # metadata timestamps
                new_part, new_broker, node_id = self._get_shard_broker(
                    shard_range, broker.root_path, policy_index, force=True)
            except DeviceUnavailable as duex:
                self.logger.warning(str(duex))
                self.logger.increment('failure')
                self._increment_stat('cleaved', 'failure')
                return False

            with new_broker.sharding_lock():
                for source_broker in brokers:
                    for objects in self.yield_objects(
                            source_broker, shard_range, policy_index):
                        new_broker.merge_items(objects)

            if shrinking:
                # When shrinking, include deleted own (donor) shard range in
                # the replicated db so that when acceptor next updates root it
                # will atomically update its namespace *and* delete the donor.
                # Don't do this when sharding a shard because the donor
                # namespace should not be deleted until all shards are cleaved.
                if own_shard_range.state != ShardRange.SHARDED:
                    own_shard_range = own_shard_range.copy(
                        timestamp=Timestamp.now(), state=ShardRange.SHARDED,
                        deleted=1)
                    broker.merge_shard_ranges([own_shard_range])
                new_broker.merge_shard_ranges([own_shard_range])
            elif shard_range.state == ShardRange.CREATED:
                # Note: deliberately not using update_state to ensure stats are
                # modified before state is changed.
                # The shard range object stats may have changed since the shard
                # range was found, so update with stats of objects actually
                # copied to the shard broker. Only do this the first time each
                # shard range is cleaved and if the source namespace includes
                # the entire shard range; when shrinking, the source namespace
                # is smaller than the existing acceptor shard range to which we
                # are cleaving and the source stats are therefore incomplete.
                info = new_broker.get_info()
                shard_range.update_meta(
                    info['object_count'], info['bytes_used'])
                if broker.is_root_container():
                    # skip CLEAVED state and set to ACTIVE; this shard range
                    # is considered authoritative in the root namespace.
                    shard_range.update_state(ShardRange.ACTIVE)
                else:
                    # set to CLEAVED state; this shard range is considered
                    # authoritative in the shard namespace; it is not
                    # authoritative in the root namespace until *all* shards
                    # have been cleaved and its state is then set to ACTIVE.
                    shard_range.update_state(ShardRange.CLEAVED)
                new_broker.merge_shard_ranges([shard_range])

            ranges_done.append(shard_range)

            self.logger.info('Replicating new shard container %s/%s for %s',
                             new_broker.account, new_broker.container,
                             new_broker.get_own_shard_range())
            self.cpool.spawn(
                self._replicate_object, new_part, new_broker.db_file, node_id)
            self.logger.info('Cleaved %s/%s for shard range %s.',
                             broker.account, broker.container, shard_range)
            self.logger.increment('sharded')
            self._increment_stat('cleaved', 'success')
            elapsed = round(time.time() - start, 3)
            self._min_stat('cleaved', 'min_time', elapsed)
            self._max_stat('cleaved', 'max_time', elapsed)

        if ranges_done:
            broker.merge_shard_ranges(ranges_done)
            cleave_context['cursor'] = str(ranges_done[-1].upper)
            if ranges_done[-1].upper >= own_shard_range.upper:
                # cleaving complete, reset cursor
                cleave_context['done'] = True
            broker.dump_cleave_context(cleave_context)
        else:
            self.logger.warning('No progress made in _cleave()!')

        # TODO: a finite timeout might be a good idea
        self.cpool.waitall(None)
        return len(ranges_done) == len(ranges_todo)

    def run_forever(self, *args, **kwargs):
        """Run the container sharder until stopped."""
        self.reported = time.time()
        time.sleep(random() * self.interval)
        while True:
            begin = time.time()
            try:
                self._one_shard_cycle()
            except (Exception, Timeout):
                self.logger.increment('errors')
                self.logger.exception('Exception in sharder')
            elapsed = time.time() - begin
            self.logger.info(
                'Container sharder cycle completed: %.02fs', elapsed)
            if elapsed < self.interval:
                time.sleep(self.interval - elapsed)

    def run_once(self, *args, **kwargs):
        """Run the container sharder once."""
        self.logger.info('Begin container sharder "once" mode')
        override_devices = list_from_csv(kwargs.get('devices'))
        override_partitions = list_from_csv(kwargs.get('partitions'))
        begin = self.reported = time.time()
        self._one_shard_cycle(override_devices=override_devices,
                              override_partitions=override_partitions)
        elapsed = time.time() - begin
        self.logger.info(
            'Container sharder "once" mode completed: %.02fs', elapsed)


class RangeLink(object):
    """
    A linked list link node but can store more then 1 link to the next
    range so we can follow forks
    """
    def __init__(self, shard_range, upper=None):
        self._shard_range = shard_range
        self._upper = upper or []

    @property
    def shard_range(self):
        return self._shard_range

    @property
    def upper(self):
        return self._upper

    @upper.setter
    def upper(self, item):
        self._upper = item


class RangeAnalyser(object):
    """Analyses a list of ShardRange objects to find the newest path,
    determine any gaps and will return a generated list of paths present.
    """
    def __init__(self):
        self._reset()

    def _reset(self):
        self.gaps = []
        self.complete = []
        self.incomplete = []
        self.newest = {}
        self.path = []
        self.paths = {'i': self.incomplete, 'c': self.complete}

    def _build(self, ranges):
        ranges.sort()
        self.path = []
        upto = {'': self.path}
        for r in ranges:
            rl = RangeLink(r)
            if str(r.lower) not in upto:
                self.gaps.append(rl)
            else:
                upto[str(r.lower)].append(rl)

            if r.upper:
                if str(r.upper) in upto:
                    rl.upper = upto[str(r.upper)]
                else:
                    upto[str(r.upper)] = rl.upper

    def _post_result(self, newest, complete, result):
        idx = None
        if complete:
            idx = 'c%d' % len(self.complete)
            self.complete.append(result)
        else:
            matched = []
            if result[0].lower is not None:
                # This incomplete has a gap at the start, it _may_ be the
                # end of another incomplete.
                matched = [(i, r) for i, r in enumerate(self.incomplete)
                           if r[-1].upper and r[-1] < result[0]]
                if matched:
                    matched = sorted(matched, key=lambda x: x[-1][-1],
                                     reverse=True)
                    i, match = matched[0]
                    match.extend(result)
                    result = match
                    idx = 'i%d' % i
                    # if this segment makes it newer we need to update the
                    # paths newest value
                    current = [(ts, idxs) for ts, idxs in self.newest.items()
                               if idx in idxs]
                    if current:
                        ts = current[0][0]
                        newest = max(ts, newest)
                        if newest != ts:
                            # remove the existing entry
                            if len(self.newest[ts]) > 1:
                                self.newest[ts].remove(idx)
                            else:
                                self.newest.pop(ts)
                        else:
                            return
            if not matched:
                idx = 'i%d' % len(self.incomplete)
                self.incomplete.append(result)

        if self.newest.get(newest):
            self.newest[newest].append(idx)
        else:
            self.newest[newest] = [idx]

    def _walk(self, rangelink, ts, result):
        newest = max(ts, rangelink.shard_range.timestamp)
        result.append(rangelink.shard_range)
        if not rangelink.upper:
            complete = (rangelink.shard_range.upper == '')
            return newest, complete, result
        elif len(rangelink.upper) > 1:
            for i, rl in enumerate(rangelink.upper):
                if i == 0:
                    # We need 1 path to send back to potentially follow other
                    # branches
                    continue
                new_res = list(result)
                new, complete, res = self._walk(rl, newest, new_res)
                self._post_result(new, complete, res)
            return self._walk(rangelink.upper[0], newest, result)
        else:
            return self._walk(rangelink.upper[0], newest, result)

    def _scan(self):
        try:
            for r in self.path:
                newest, complete, result = self._walk(
                    r, r.shard_range.timestamp, [])
                self._post_result(newest, complete, result)

            for r in self.gaps:
                newest, complete, result = self._walk(
                    r, r.shard_range.timestamp, [])
                self._post_result(newest, False, result)

            self._break_ties()
        except Exception as ex:
            # TODO: either stop wrapping or include a traceback -- errors like
            #
            #   <lambda>() takes exactly 1 argument (2 given)
            #
            # aren't super helpful on their own
            raise RangeAnalyserException('Failed to find a correct set of '
                                         'ranges: %s' % str(ex))

    def _break_ties(self):
        # Idea here, is that the may be a newer change to the list of ranges
        # and this is where the same TS is coming from. So lets get the
        # difference and compare only whats different on tied paths.
        def to_timestamps(p):
            return [item.timestamp for item in p]

        tmp_newest = self.newest.copy()
        for ts, indexes in ((k, v) for k, v in tmp_newest.items()
                            if len(v) > 1):
            paths = [(idx, self.paths[idx[:1]][int(idx[1:])])
                     for idx in indexes]

            timestamps = []
            for idx, path in paths:
                dif = list(map(set(path).difference,
                               [v for p, v in paths if p != idx]))
                tss = list(map(to_timestamps, dif))
                max_tss = sorted(map(max, tss), reverse=True)
                timestamps.append((
                    idx, ''.join(ts.internal for ts in max_tss)))

            timestamps.sort(key=lambda x: x[-1])
            self.newest.pop(ts)
            timestamp = Timestamp(ts)
            for idx, _junk in timestamps:
                timestamp.offset += 1
                self.newest[timestamp.internal] = [idx]

    def _pick(self):
        newest_keys = sorted(self.newest.keys(), reverse=True)

        for key in newest_keys:
            idx = self.newest[key][0]
            path = self.paths[idx[:1]][int(idx[1:])]
            # yeild <path>, <set of items from ranges not in path>, completed
            yield sorted(path), self._ranges.difference(set(path)), \
                idx.startswith('c')

    def analyse(self, ranges):
        """Analyse a list of ShardRanges for a list of paths and gaps.

        Yields a tuple comprising the determined paths from newest to oldest,
        a set of any unused ShardRanges and a boolean indicating if the path
        is complete or not.

        :param ranges:
        :return: Tuple of the form
            (<path list>, <set of other ShardRanges>, <bool complete>).
        """
        self._reset()
        self._ranges = set(ranges)
        self._build(ranges)
        self._scan()
        return self._pick()
