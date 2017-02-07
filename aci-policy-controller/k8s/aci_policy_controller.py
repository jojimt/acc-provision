# Copyright (c) 2015-2016 Tigera Inc.  All rights reserved.
# Copyright (c) 2016 Cisco Systems, Inc.  All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
# 
#  http://www.apache.org/licenses/LICENSE-2.0
# 
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

# Controller for syncing kubernetes network policy with ACI using the
# ACI integration module.
#
# This is based on the Calico k8s-policy controller which can be found at:
# https://github.com/projectcalico/k8s-policy

import six

import logging
import os
import requests
import sys
import simplejson as json
import time
import aim

if six.PY2:
    import Queue as queue
else:
    import queue

from threading import Thread
from constants.logging import *
from constants.k8s import *
from constants.aim import *

import sqlalchemy
from aim import aim_manager
from aim.api import resource as aim_resource
from aim import context as aim_context
from aim import utils as aim_utils

from . network_policy import (add_update_network_policy,
                            delete_network_policy)
from . namespace import add_update_namespace, delete_namespace
from . pod import add_update_pod, delete_pod
import policy_cache
import aci_setup

_log = logging.getLogger("__main__")

# Raised upon receiving an error from the Kubernetes API.
class KubernetesApiError(Exception):
    pass

class Controller(object):
    def __init__(self):
        self._event_queue = queue.Queue(maxsize=MAX_QUEUE_SIZE)
        """
        Queue to populate with events from API watches.
        """

        self.k8s_api = os.environ.get("K8S_API", DEFAULT_API)
        """
        Scheme, IP and port of the Kubernetes API.
        """

        self.auth_token = os.environ.get("K8S_AUTH_TOKEN", read_token_file())
        """
        Auth token to use when accessing the API.
        """
        _log.debug("Using auth token: %s", self.auth_token)

        self.ca_crt_exists = os.path.exists(CA_CERT_PATH)
        """
        True if a CA cert has been mounted by Kubernetes.
        """

        self.client_key = os.environ.get("K8S_CLIENT_KEY", None)
        """
        Initialize to the client key to use to connect to API
        """

        self.client_cert = os.environ.get("K8S_CLIENT_CERT", None)
        """
        Initialize to the client certificate to use to connect to API
        """

        self._leader_election_url = os.environ.get("ELECTION_URL",
                                                   "http://127.0.0.1:4040/")
        """
        Use this URL to get leader election status from the sidecar container.
        """

        elect = os.environ.get("LEADER_ELECTION", "false")
        self._leader_elect = elect.lower() ==  "true"
        """
        Whether or not leader election is enabled.  If set to False, this
        policy controller will assume it is the only instance.
        """

        self._aim = aim_manager.AimManager()
        """
        AIM manager for configuring ACI policy
        """

        connection_str = os.environ.get("AIM_DB_CONNECTION", AIM_DB_CONNECTION)
        engine = sqlalchemy.create_engine(connection_str)
        session_maker = sqlalchemy.orm.sessionmaker(bind=engine, autocommit=True)
        self._aim_context = aim_context.AimContext(db_session=session_maker())
        """
        AIM context for making changes to AIM database
        """

        self._aci_tenant = os.environ.get("ACI_TENANT", ACI_TENANT)
        
        self._handlers = {}
        """
        Keeps track of which handlers to execute for various events.
        """

        self._policy_cache = policy_cache.PolicyCache()
        """
        Cache kubernetes policies needed to compute ACI policy
        """

        # Handlers for NetworkPolicy events.
        self.add_handler(RESOURCE_TYPE_NETWORK_POLICY, TYPE_ADDED,
                         add_update_network_policy)
        self.add_handler(RESOURCE_TYPE_NETWORK_POLICY, TYPE_MODIFIED,
                         add_update_network_policy)
        self.add_handler(RESOURCE_TYPE_NETWORK_POLICY, TYPE_DELETED,
                         delete_network_policy)

        # Handlers for Namespace events.
        self.add_handler(RESOURCE_TYPE_NAMESPACE, TYPE_ADDED,
                         add_update_namespace)
        self.add_handler(RESOURCE_TYPE_NAMESPACE, TYPE_MODIFIED,
                         add_update_namespace)
        self.add_handler(RESOURCE_TYPE_NAMESPACE, TYPE_DELETED,
                         delete_namespace)

        # Handlers for Pod events.
        self.add_handler(RESOURCE_TYPE_POD, TYPE_ADDED,
                         add_update_pod)
        self.add_handler(RESOURCE_TYPE_POD, TYPE_MODIFIED,
                         add_update_pod)
        self.add_handler(RESOURCE_TYPE_POD, TYPE_DELETED,
                         delete_pod)

    def add_handler(self, resource_type, event_type, handler):
        """
        Adds an event handler for the given event type (ADD, DELETE) for the
        given resource type.

        :param resource_type: The type of resource that this handles.
        :param event_type: The type of event that this handles.
        :param handler: The callable to execute when events are received.
        :return None
        """
        _log.debug("Setting %s %s handler: %s",
                    resource_type, event_type, handler)
        key = (resource_type, event_type)
        self._handlers[key] = handler

    def get_handler(self, resource_type, event_type):
        """
        Gets the correct handler.

        :param resource_type: The type of resource that needs handling.
        :param event_type: The type of event that needs handling.
        :return None
        """
        key = (resource_type, event_type)
        _log.debug("Looking up handler for event: %s", key)
        return self._handlers[key]

    def initialize(self):
        """
        Initialize the environment to prepare for kubernetes policy
        """
        aci_setup.aci_setup(self)
    
    def run(self):
        """
        Controller.run() is called at program init to spawn watch threads,
        Loops to read responses from the Queue as they come in.
        """
        _log.info("Leader election enabled? %s", self._leader_elect)
        if self._leader_elect:
            # Wait until we've been elected leader to start.
            self._wait_for_leadership()
            self._start_leader_thread()

        self.initialize()
        
        # Read initial state from Kubernetes API.
        self.start_workers()

        # Loop and read updates from the queue.
        self.read_updates()

    def _wait_for_leadership(self):
        """
        Loops until this controller has been elected leader.
        """
        _log.info("Waiting for this controller to be elected leader")
        while True:
            try:
                is_leader = self._is_leader()
            except requests.exceptions.ConnectionError:
                # During startup, the leader election container
                # might not be up yet.  Handle this case gracefully.
                _log.info("Waiting for leader election container")
            else:
                # Successful response from the leader election container.
                # Check if we are the elected leader.
                if is_leader:
                    _log.info("We have been elected leader")
                    break
            time.sleep(1)

    def _start_leader_thread(self):
        """
        Starts a thread which periodically checks if this controller is the leader.
        If determined that we are no longer the leader, exit.
        """
        t = Thread(target=self._watch_leadership)
        t.daemon = True
        t.start()
        _log.info("Started leader election watcher")

    def _watch_leadership(self):
        """
        Watches to see if this policy controller is still the elected leader.
        If no longer the elected leader, exits.
        """
        _log.info("Watching for leader election changes")
        while True:
            try:
                if not self._is_leader():
                    _log.warning("No longer the elected leader - exiting")
                    os._exit(1)
                time.sleep(1)
            except Exception:
                _log.exception("Exception verifying leadership - exiting")
                os._exit(1)

    def start_workers(self):
        """
        Starts the worker threads which manage each Kubernetes
        API resource.
        """
        resources = [RESOURCE_TYPE_NETWORK_POLICY,
                     RESOURCE_TYPE_NAMESPACE,
                     RESOURCE_TYPE_POD]

        # For each resource type, start a thread which syncs it from the
        # kubernetes API.
        for resource_type in resources:
            t = Thread(target=self._manage_resource, args=(resource_type,))
            t.daemon = True
            t.start()
            _log.info("Started worker thread for: %s", resource_type)

    def read_updates(self):
        """
        Reads from the update queue.

        An update on the queue must be a tuple of:
          (event_type, resource_type, resource)

        Where:
          - event_type: Either "ADDED", "MODIFIED", "DELETED", "ERROR"
          - resource_type: e.g "Namespace", "Pod", "NetworkPolicy"
          - resource: The parsed json resource from the API matching
                      the given resource_type.
        """
        while True:
            try:
                # Wait for an update on the event queue.
                _log.debug("Reading from event queue")
                update = self._event_queue.get(block=True)
                event_type, resource_type, resource = update

                # We've recieved an update - process it.
                #_log.debug("Read event: %s, %s, %s",
                #           event_type,
                #           resource_type,
                #           json.dumps(resource, indent=2))
                self._process_update(event_type,
                                     resource_type,
                                     resource)
            except KeyError:
                # We'll hit this if we fail to parse an invalid update.
                _log.exception("Invalid update: %s", update)
            finally:
                self._event_queue.task_done()

                # Log out when the queue is empty.
                if self._event_queue.empty():
                    _log.info("Emptied the event queue")

    def _process_update(self, event_type, resource_type, resource):
        """
        Takes an event updates our state accordingly.
        """
        _log.debug("Processing '%s' for kind '%s'", event_type, resource_type)

        # Determine the key for this object using namespace and name.
        # This is simply used for easy identification in logs, etc.
        name = resource["metadata"]["name"]
        namespace = resource["metadata"].get("namespace")
        key = (namespace, name)

        # Call the right handler.
        try:
            handler = self.get_handler(resource_type, event_type)
        except KeyError:
            _log.warning("No %s handlers for: %s",
                         event_type, resource_type)
        else:
            try:
                handler(self, resource)
                _log.info("Handled %s for %s: %s",
                           event_type, resource_type, key)
            except KeyError:
                _log.exception("Invalid %s: %s", resource_type,
                               json.dumps(resource, indent=2))

    def _manage_resource(self, resource_type):
        """
        Routine for a worker thread.  Syncs with API for the given resource
        and starts a watch.  If an error occurs within the watch, will re-sync
        with the API and re-start the watch.
        """
        while True:
            try:
                # Sync existing resources for this type.
                resource_version = self._sync_resources(resource_type)

                # Start a watch from the latest resource_version.
                self._watch_resource(resource_type, resource_version)
            except (requests.exceptions.ConnectionError,
                    requests.exceptions.ChunkedEncodingError):
                _log.exception("Connection error querying: %s", resource_type)
            except requests.exceptions.HTTPError:
                _log.exception("HTTP error querying: %s", resource_type)
            except KubernetesApiError:
                _log.debug("Kubernetes API error managing %s", resource_type)
            except queue.Full:
                _log.exception("Event queue full")
            except Exception as e:
                _log.exception("Unhandled exception %s killed %s manager",
                               repr(e), resource_type)
            finally:
                # Sleep for a second so that we don't tight-loop.
                _log.warning("Re-starting watch on resource: %s",
                             resource_type)
                time.sleep(1)

    def _watch_resource(self, resource_type, resource_version):
        """
        Watch the given resource type starting at the given resource version.
        Add any events to the event queue.
        """
        path = WATCH_URLS[resource_type] % self.k8s_api
        _log.info("Starting watch on: %s", path)
        while True:
            # Attempt to stream API resources.
            response = self._api_get(path,
                                     stream=True,
                                     resource_version=resource_version)
            _log.debug("Watch response for %s: %s", path, response)

            # Check for successful response, raise error if not.
            if response.status_code != 200:
                raise KubernetesApiError(response.text)

            # Success - add resources to the queue for processing.
            for line in response.iter_lines():
                # Filter out keep-alive new lines.
                if line:
                    _log.debug("Read line: %s", line)
                    parsed = json.loads(line)

                    # Check if we've encountered an error.  If so,
                    # raise an exception.
                    if parsed["type"] == TYPE_ERROR:
                        _log.error("Received error from API: %s",
                                   json.dumps(parsed, indent=2))
                        raise KubernetesApiError()

                    # Get the important information from the event.
                    event_type = parsed["type"]
                    resource_type = parsed["object"]["kind"]
                    resource = parsed["object"]

                    # Successful update - send to the queue.
                    _log.info("%s %s: %s to queue (%s) (%s)",
                              event_type,
                              resource_type,
                              resource["metadata"]["name"],
                              self._event_queue.qsize(),
                              time.time())

                    update = (event_type, resource_type, resource)
                    self._event_queue.put(update,
                                          block=True,
                                          timeout=QUEUE_PUT_TIMEOUT)

                    # Extract the latest resource version.
                    new_ver = resource["metadata"]["resourceVersion"]
                    _log.debug("Update resourceVersion, was: %s, now: %s",
                               resource_version, new_ver)
                    resource_version = new_ver

    def _sync_resources(self, resource_type):
        """
        Syncs with the API and determines the latest resource version.
        Adds API objects to the event queue and
        returns the latest resourceVersion.
        Raises an Exception if unable to access the API.
        """
        # Get existing resources from the API.
        _log.info("Syncing '%s' objects", resource_type)
        url = GET_URLS[resource_type] % self.k8s_api
        resp = self._api_get(url, stream=False)
        _log.debug("Response: %s", resp)

        # If we hit an error, raise it.
        if resp.status_code != 200:
            _log.error("Error querying API: %s", resp.json())
            raise KubernetesApiError("Failed to query resource: %s" % resource_type)

        # Get the list of existing API objects from the response, as
        # well as the latest resourceVersion.
        resources = resp.json()["items"]
        metadata = resp.json().get("metadata", {})
        resource_version = metadata.get("resourceVersion")
        _log.debug("%s metadata: %s", resource_type, metadata)

        # Add the existing resources to the queue to be processed.
        # Treat as a MODIFIED event to trigger updates which may not always
        # occur on ADDED.
        _log.info("%s existing %s(s) - add to queue",
                  len(resources), resource_type)
        for resource in resources:
            _log.debug("Queueing update: %s", resource)
            update = (TYPE_MODIFIED, resource_type, resource)
            self._event_queue.put(update,
                                  block=True,
                                  timeout=QUEUE_PUT_TIMEOUT)

        _log.info("Done getting %s(s) - new resourceVersion: %s",
                  resource_type, resource_version)
        return resource_version

    def _api_get(self, path, stream, resource_version=None):
        """
        Get or stream from the API, given a resource.

        :param path: The API path to get.
        :param stream: Whether to return a single object or a stream.
        :param resource_version: The resourceVersion at which to
        start the stream.
        :return: A requests Response object
        """
        # Append the resource version - this indicates where the
        # watch should start.
        _log.debug("Getting API resources '%s' at version '%s'. stream=%s",
                  path, resource_version, stream)
        if resource_version:
            path += "?resourceVersion=%s" % resource_version

        session = requests.Session()
        if self.client_key is not None and self.client_cert is not None:
            session.cert = (self.client_cert, self.client_key)
        if self.auth_token:
            session.headers.update({'Authorization': 'Bearer ' + self.auth_token})
        verify = CA_CERT_PATH if self.ca_crt_exists else False
        return session.get(path, verify=verify, stream=stream)

    def _is_leader(self):
        """
        Returns True if this policy controller instance has been elected leader,
        False otherwise.
        """
        _log.debug("Checking if we are the elected leader.")
        response = requests.get(self._leader_election_url)
        response = response.json()

        # Determine if we're the leader.
        our_name = os.environ.get("HOSTNAME")
        leader_name = response["name"]
        _log.debug("Elected leader is: %s. We are: %s", leader_name, our_name)
        return our_name == leader_name


def read_token_file():
    """
    Gets the API access token from the serviceaccount file.
    """
    file_path = "/var/run/secrets/kubernetes.io/serviceaccount/token"
    _log.debug("Getting ServiceAccount token from: %s", file_path)
    if not os.path.exists(file_path):
        _log.warning("No ServiceAccount token found on disk")
        return None

    with open(file_path, "r") as f:
        token = f.read().replace('\n', '')
    _log.debug("Found ServiceAccount token: %s", token)
    return token


def configure_etc_hosts():
    """
    Reads the Kubernetes service environment variables and configures
    /etc/hosts accordingly.

    We need to do this for a combination of two reasons:
      1) When TLS is enabled, SSL verification requires that a hostname
         is used when initiating a connection.
      2) DNS lookups may fail at start of day, because this controller is
         responsible for allowing access to the DNS pod, but it must access
         the k8s API to do so, causing a dependency loop.
    """
    k8s_host = os.environ.get(K8S_SERVICE_HOST, "10.100.0.1")
    with open("/etc/hosts", "a") as f:
        f.write("%s    kubernetes.default\n" % k8s_host)
    _log.info("Appended 'kubernetes.default  -> %s' to /etc/hosts", k8s_host)

def policy_main():
    # Configure logging.
    log_level = os.environ.get("LOG_LEVEL", "info").upper()
    formatter = logging.Formatter(LOG_FORMAT)
    stdout_hdlr = logging.StreamHandler(sys.stderr)
    stdout_hdlr.setFormatter(formatter)
    _log.addHandler(stdout_hdlr)
    _log.setLevel(log_level)

    if os.environ.get("CONFIGURE_ETC_HOSTS", "false").lower() == "true":
        # Configure /etc/hosts with Kubernetes API.
        # Don't do this by default, since it is recommended to run
        # this pod using host networking on the master.
        _log.info("Configuring /etc/hosts")
        configure_etc_hosts()

    _log.info("Beginning execution")
    Controller().run()

if __name__ == '__main__':
    policy_main();