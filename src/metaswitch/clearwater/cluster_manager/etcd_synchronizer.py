#!/usr/bin/python

# Project Clearwater - IMS in the Cloud
# Copyright (C) 2015 Metaswitch Networks Ltd
#
# This program is free software: you can redistribute it and/or modify it
# under the terms of the GNU General Public License as published by the
# Free Software Foundation, either version 3 of the License, or (at your
# option) any later version, along with the "Special Exception" for use of
# the program along with SSL, set forth below. This program is distributed
# in the hope that it will be useful, but WITHOUT ANY WARRANTY;
# without even the implied warranty of MERCHANTABILITY or FITNESS FOR
# A PARTICULAR PURPOSE.  See the GNU General Public License for more
# details. You should have received a copy of the GNU General Public
# License along with this program.  If not, see
# <http://www.gnu.org/licenses/>.
#
# The author can be reached by email at clearwater@metaswitch.com or by
# post at Metaswitch Networks Ltd, 100 Church St, Enfield EN2 6BQ, UK
#
# Special Exception
# Metaswitch Networks Ltd  grants you permission to copy, modify,
# propagate, and distribute a work formed by combining OpenSSL with The
# Software, or a work derivative of such a combination, even if such
# copying, modification, propagation, or distribution would otherwise
# violate the terms of the GPL. You must comply with the GPL in all
# respects for all of the code used other than OpenSSL.
# "OpenSSL" means OpenSSL toolkit software distributed by the OpenSSL
# Project and licensed under the OpenSSL Licenses, or a work based on such
# software and licensed under the OpenSSL Licenses.
# "OpenSSL Licenses" means the OpenSSL License and Original SSLeay License
# under which the OpenSSL Project distributes the OpenSSL toolkit software,
# as those licenses appear in the file LICENSE-OPENSSL.

import json

import constants
from .synchronization_fsm import SyncFSM
from metaswitch.clearwater.etcd_shared.common_etcd_synchronizer import CommonEtcdSynchronizer
from .cluster_state import ClusterInfo
import logging

_log = logging.getLogger(__name__)


class EtcdSynchronizer(CommonEtcdSynchronizer):
    def __init__(self, plugin, ip, etcd_ip=None, force_leave=False):
        CommonEtcdSynchronizer.__init__(self, plugin, ip, etcd_ip)
        self._fsm = SyncFSM(self._plugin, self._ip)
        self._leaving_flag = False
        self.force_leave = force_leave

    def key(self):
        return self._plugin.key()

    def is_running(self):
        return self._fsm.is_running()

    def default_value(self):
        return "{}"

    def main(self):
        # Continue looping while the FSM is running.
        while self._fsm.is_running():
            # This blocks on changes to the cluster in etcd.
            _log.debug("Waiting for state change from etcd")
            etcd_value = self.update_from_etcd()
            if self._terminate_flag:
                break
            if etcd_value is not None:
                _log.info("Got new state %s from etcd" % etcd_value)
                cluster_info = ClusterInfo(etcd_value)

                # This node can only leave the cluster if the cluster is in a
                # stable state. Check the leaving flag and the cluster state. If
                # necessary, set this node to WAITING_TO_LEAVE. Otherwise, kick
                # the FSM.
                if (self._leaving_flag and
                        cluster_info.can_leave(self.force_leave)):
                    _log.info("Cluster is in a stable state, so leaving the cluster now")
                    new_state = constants.WAITING_TO_LEAVE
                else:
                    new_state = self._fsm.next(cluster_info.local_state(self._ip),
                                               cluster_info.cluster_state,
                                               cluster_info.view)

                # If we have a new state, try and write it to etcd.
                if new_state is not None:
                    self.write_to_etcd(cluster_info, new_state)
                else:
                    _log.debug("No state change")
            else:
                _log.warning("read_from_etcd returned None, " +
                             "indicating a failure to get data from etcd")

        _log.info("Quitting FSM")
        self._fsm.quit()

    # This node has been asked to leave the cluster. Check if the cluster is in
    # a stable state, in which case we can leave. Otherwise, set a flag and
    # leave at the next available opportunity.
    def leave_cluster(self):
        _log.info("Trying to leave the cluster - plugin %s" %
                  self._plugin.__class__.__name__)

        if not self._plugin.should_be_in_cluster():
            _log.info("No need to leave remote cluster - just exit")
            self._terminate_flag = True
            return

        etcd_result, idx = self.read_from_etcd(wait=False)
        cluster_info = ClusterInfo(etcd_result)

        if cluster_info.can_leave(self.force_leave):
            _log.info("Cluster is in a stable state, so leaving the cluster immediately")
            self.write_to_etcd(cluster_info, constants.WAITING_TO_LEAVE)
        else:
            _log.info("Can't leave the cluster immediately - " +
                      "will do so when the cluster next stabilises")
            self._leaving_flag = True

    def mark_node_failed(self):
        if not self._plugin.should_be_in_cluster():
            _log.debug("No need to mark failure in remote cluster - doing nothing")
            # We're just monitoring this cluster, not in it, so leaving is a
            # no-op
            return

        etcd_result, idx = self.read_from_etcd(wait=False)
        if etcd_result is not None:
            _log.warning("Got result of None from read_from_etcd")
        cluster_info = ClusterInfo(etcd_result)

        self.write_to_etcd(cluster_info, constants.ERROR)

    # Write the new cluster view to etcd. We may be expecting to create the key
    # for the first time.
    def write_to_etcd(self, cluster_info, new_state, with_index=None):
        index = with_index or self._index
        cluster_view = cluster_info.view.copy()

        # Update the cluster view based on new state information. If new_state
        # is a string then it refers to the new state of the local node.
        # Otherwise, it is an overall picture of the new cluster.
        if new_state == constants.DELETE_ME:
            del cluster_view[self._ip]
        elif isinstance(new_state, str):
            cluster_view[self._ip] = new_state
        elif isinstance(new_state, dict):
            cluster_view = new_state

        _log.debug("Writing state %s into etcd" % cluster_view)
        json_data = json.dumps(cluster_view)

        try:
            if index:
                self._client.write(self.key(), json_data, prevIndex=index)
            else:
                self._client.write(self.key(), json_data, prevExist=False)

            # We may have just successfully set the local node to
            # WAITING_TO_LEAVE, in which case we no longer need the leaving
            # flag.
            self._leaving_flag = False
        except ValueError:
            _log.debug("Contention on etcd write - new_state is {}".format(new_state))
            # Our etcd write failed because someone got there before us.

            if isinstance(new_state, str):
                # We're just trying to update our own state, so it may be safe
                # to take the new state, update our own state in it, and retry.
                (etcd_result, idx) = self.read_from_etcd(wait=False)
                updated_cluster_info = ClusterInfo(etcd_result)

                # This isn't safe if someone else has changed our state for us,
                # or the overall deployment state has changed (in which case we
                # may want to change our state to something else, so check for
                # that.
                if (new_state in [constants.ERROR, constants.DELETE_ME] or
                    ((updated_cluster_info.local_state(self._ip) ==
                     cluster_info.local_state(self._ip)) and
                    (updated_cluster_info.cluster_state ==
                     cluster_info.cluster_state))):
                    _log.debug("Retrying contended write with updated value")
                    self.write_to_etcd(updated_cluster_info,
                                       new_state,
                                       with_index=idx)
        except Exception as e:
            # Catch-all error handler (for invalid requests, timeouts, etc -
            # unset our state and start over.
            _log.error("{} caught {!r} when trying to write {} with index {}"
                       " - pause before retrying"
                       .format(self._ip, e, json_data, self._index))
            # Setting last_cluster_view to None means that the next successful
            # read from etcd will trigger the state machine, which will mean
            # that any necessary work/state changes get retried.
            self._last_value, self._last_index = None, None
            # Sleep briefly to avoid hammering a failed server
            self.pause()
