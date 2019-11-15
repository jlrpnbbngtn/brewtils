# -*- coding: utf-8 -*-
import abc
import json
import logging
import sys
import threading
from concurrent.futures.thread import ThreadPoolExecutor

import six
from requests import ConnectionError as RequestsConnectionError

import brewtils.plugin
from brewtils.errors import (
    DiscardMessageException,
    parse_exception_as_json,
    RepublishRequestException,
    RestConnectionError,
    RestClientError,
    RequestProcessingError,
)
from brewtils.models import Request
from brewtils.schema_parser import SchemaParser


class RequestProcessor(object):
    """Class responsible for coordinating Request processing

    The RequestProcessor is responsible for the following:
    - Defining on_message_received callback that will be invoked by the PikaConsumer
    - Parsing the request
    - Invoking the command on the target
    - Formatting the output
    - Reporting request updates to Beergarden (using a RequestUpdater)

    Args:
        target: Incoming requests will be invoked on this object
        updater: RequestUpdater that will be used for updating requests
        validation_funcs: List of functions that will called before invoking a command
        logger: A logger
        unique_name: The Plugin's unique name
        max_workers: Max number of threads to use in the executor pool

    """

    def __init__(
        self,
        target,
        updater,
        validation_funcs=None,
        logger=None,
        unique_name=None,
        max_workers=None,
    ):
        self.logger = logger or logging.getLogger(__name__)

        self._target = target
        self._updater = updater
        self._unique_name = unique_name
        self._validation_funcs = validation_funcs or []
        self._pool = ThreadPoolExecutor(max_workers=max_workers)

    def on_message_received(self, message, headers):
        """Callback function that will be invoked for received messages

        This will attempt to parse the message and then run the parsed Request through
        all validation functions that this RequestProcessor knows about.

        If the request parses cleanly and passes validation it will be submitted to this
        RequestProcessor's ThreadPoolExecutor for processing.

        Args:
            message: The message string
            headers: The header dictionary

        Returns:
            A future that will complete when processing finishes

        Raises:
            DiscardMessageException: The request failed to parse correctly
            RequestProcessException: Validation failures should raise a subclass of this
        """
        request = self._parse(message)

        for func in self._validation_funcs:
            func(request)

        # This message has already been processed, all it needs to do is update
        if request.status in Request.COMPLETED_STATUSES:
            return self._pool.submit(self._updater.update_request, request, headers)
        else:
            return self._pool.submit(
                self.process_message, self._target, request, headers
            )

    def process_message(self, target, request, headers):
        """Process a message. Intended to be run on an Executor.

        Will set the status to IN_PROGRESS, invoke the command, and set the final
        status / output / error_class.

        Args:
            target: The object to invoke received commands on
            request: The parsed Request
            headers: Dictionary of headers from the `PikaConsumer`

        Returns:
            None
        """
        request.status = "IN_PROGRESS"
        self._updater.update_request(request, headers)

        try:
            # Set request context so this request will be the parent of any
            # generated requests and update status We also need the host/port of
            #  the current plugin. We currently don't support parent/child
            # requests across different servers.
            brewtils.plugin.request_context.current_request = request

            output = self._invoke_command(target, request)
        except Exception as ex:
            self.logger.log(
                getattr(ex, "_bg_error_log_level", logging.ERROR),
                "Plugin %s raised an exception while processing request %s: %s",
                self._unique_name,
                str(request),
                ex,
                exc_info=not getattr(ex, "_bg_suppress_stacktrace", False),
            )
            request.status = "ERROR"
            request.output = self._format_error_output(request, ex)
            request.error_class = type(ex).__name__
        else:
            request.status = "SUCCESS"
            request.output = self._format_output(output)

        self._updater.update_request(request, headers)

    def shutdown(self):
        """Stop the RequestProcessor"""
        # Finish all current actions
        self._pool.shutdown(wait=True)

        # Give the updater a chance to shutdown
        self._updater.shutdown()

    def _parse(self, message):
        """Parse a message using the standard SchemaParser

        Args:
            message: The raw (json) message body

        Returns:
            A Request model

        Raises:
            DiscardMessageException: The request failed to parse correctly
        """
        try:
            return SchemaParser.parse_request(message, from_string=True)
        except Exception as ex:
            self.logger.exception(
                "Unable to parse message body: {0}. Exception: {1}".format(message, ex)
            )
            raise DiscardMessageException("Error parsing message body")

    @staticmethod
    def _invoke_command(target, request):
        """Invoke the function named in request.command

        Args:
            target: The object to search for the function implementation.
            request: The request to process

        Returns:
            The output of the function call

        Raises:
            RequestProcessingError: The specified target does not define a
                callable implementation of request.command
        """
        if not callable(getattr(target, request.command, None)):
            raise RequestProcessingError(
                "Could not find an implementation of command '%s'" % request.command
            )

        parameters = request.parameters or {}

        return getattr(target, request.command)(**parameters)

    @staticmethod
    def _format_error_output(request, exc):
        if request.is_json:
            return parse_exception_as_json(exc)
        else:
            return str(exc)

    @staticmethod
    def _format_output(output):
        if isinstance(output, six.string_types):
            return output

        try:
            return json.dumps(output)
        except (TypeError, ValueError):
            return str(output)


@six.add_metaclass(abc.ABCMeta)
class RequestUpdater(object):
    @abc.abstractmethod
    def update_request(self, request, headers):
        pass

    @abc.abstractmethod
    def shutdown(self):
        pass


class NoopUpdater(RequestUpdater):
    """RequestUpdater implementation that explicitly does not update."""

    def __init__(self, *args, **kwargs):
        pass

    def update_request(self, request, headers):
        pass

    def shutdown(self):
        pass


class HTTPRequestUpdater(RequestUpdater):
    """RequestUpdater implementation based around an EasyClient.

    Args:
        ez_client: EasyClient to use for communication
        shutdown_event: `threading.Event` to allow for timely shutdowns
        logger: A logger

    Keyword Args:
        logger: A logger
        max_attempts: Max number of unsuccessful updates before discarding the message
        max_timeout: Maximum amount of time (seconds) to wait between update attempts
        starting_timeout: Starting time to wait (seconds) between update attempts

    """

    def __init__(self, ez_client, shutdown_event, **kwargs):
        self.logger = kwargs.get("logger", logging.getLogger(__name__))

        self._ez_client = ez_client
        self._shutdown_event = shutdown_event

        self.max_attempts = kwargs.get("max_attempts", -1)
        self.max_timeout = kwargs.get("max_timeout", 30)
        self.starting_timeout = kwargs.get("starting_timeout", 5)

        # Tightly manage when we're in an 'error' state, aka Brew-view is down
        self.brew_view_error_condition = threading.Condition()
        self.brew_view_down = False

        self.logger.debug("Creating and starting connection poll thread")
        self.connection_poll_thread = self._create_connection_poll_thread()
        self.connection_poll_thread.start()

    def shutdown(self):
        self.logger.debug("Shutting down, about to wake any sleeping updater threads")
        with self.brew_view_error_condition:
            self.brew_view_error_condition.notify_all()

    def update_request(self, request, headers):
        """Sends a Request update to beer-garden

        Ephemeral requests do not get updated, so we simply skip them.

        If brew-view appears to be down, it will wait for brew-view to come back
         up before updating.

        If this is the final attempt to update, we will attempt a known, good
        request to give some information to the user. If this attempt fails
        then we simply discard the message.

        Args:
            request: The request to update
            headers: A dictionary of headers from the `PikaConsumer`

        Returns:
            None

        Raises:
            RepublishMessageException: The Request update failed (any reason)
        """
        if request.is_ephemeral:
            sys.stdout.flush()
            return

        with self.brew_view_error_condition:

            self._wait_for_brew_view_if_down(request)

            try:
                if not self._should_be_final_attempt(headers):
                    self._wait_if_not_first_attempt(headers)
                    self._ez_client.update_request(
                        request.id,
                        status=request.status,
                        output=request.output,
                        error_class=request.error_class,
                    )
                else:
                    self._ez_client.update_request(
                        request.id,
                        status="ERROR",
                        output="We tried to update the request, but it failed too many "
                        "times. Please check the plugin logs to figure out why the "
                        "request update failed. It is possible for this request to "
                        "have succeeded, but we cannot update beer-garden with that "
                        "information.",
                        error_class="BGGivesUpError",
                    )
            except Exception as ex:
                self._handle_request_update_failure(request, headers, ex)
            finally:
                sys.stdout.flush()

    def _wait_if_not_first_attempt(self, headers):
        if headers.get("retry_attempt", 0) > 0:
            time_to_sleep = min(
                headers.get("time_to_wait", self.starting_timeout), self.max_timeout
            )
            self._shutdown_event.wait(time_to_sleep)

    def _handle_request_update_failure(self, request, headers, exc):

        # If brew-view is down, we always want to try again
        # Yes, even if it is the 'final_attempt'
        if isinstance(exc, (RequestsConnectionError, RestConnectionError)):
            self.brew_view_down = True
            self.logger.error(
                "Error updating request status: {0} exception: {1}".format(
                    request.id, exc
                )
            )
            raise RepublishRequestException(request, headers)

        elif isinstance(exc, RestClientError):
            message = (
                "Error updating request {0} and it is a client error. Probable "
                "cause is that this request is already updated. In which case, "
                "ignore this message. If request {0} did not complete, please "
                "file an issue. Discarding request to avoid an infinite loop. "
                "exception: {1}".format(request.id, exc)
            )
            self.logger.error(message)
            raise DiscardMessageException(message)

        # Time to discard the message because we've given up
        elif self._should_be_final_attempt(headers):
            message = (
                "Could not update request {0} even with a known good status, "
                "output and error_class. We have reached the final attempt and "
                "will now discard the message. Attempted to process this "
                "message {1} times".format(request.id, headers["retry_attempt"])
            )
            self.logger.error(message)
            raise DiscardMessageException(message)

        else:
            self._update_retry_attempt_information(headers)
            self.logger.exception(
                "Error updating request (Attempt #{0}: request: {1} exception: "
                "{2}".format(headers.get("retry_attempt", 0), request.id, exc)
            )
            raise RepublishRequestException(request, headers)

    def _update_retry_attempt_information(self, headers):
        headers["retry_attempt"] = headers.get("retry_attempt", 0) + 1
        headers["time_to_wait"] = min(
            headers.get("time_to_wait", self.starting_timeout // 2) * 2,
            self.max_timeout,
        )

    def _should_be_final_attempt(self, headers):
        if self.max_attempts <= 0:
            return False

        return self.max_attempts <= headers.get("retry_attempt", 0)

    def _wait_for_brew_view_if_down(self, request):
        if self.brew_view_down and not self._shutdown_event.is_set():
            self.logger.warning(
                "Currently unable to communicate with Brew-view, about to wait "
                "until connection is reestablished to update request %s",
                request.id,
            )
            self.brew_view_error_condition.wait()

    def _create_connection_poll_thread(self):
        connection_poll_thread = threading.Thread(target=self._connection_poll)
        connection_poll_thread.daemon = True
        return connection_poll_thread

    def _connection_poll(self):
        """Periodically attempt to re-connect to beer-garden"""

        while not self._shutdown_event.wait(5):
            try:
                with self.brew_view_error_condition:
                    if self.brew_view_down:
                        try:
                            self._ez_client.get_version()
                        except Exception:
                            self.logger.debug("Brew-view reconnection attempt failure")
                        else:
                            self.logger.info(
                                "Brew-view connection reestablished, about to "
                                "notify any waiting requests"
                            )
                            self.brew_view_down = False
                            self.brew_view_error_condition.notify_all()
            except Exception as ex:
                self.logger.exception("Exception in connection poll thread: %s", ex)
