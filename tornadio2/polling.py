# -*- coding: utf-8 -*-
"""
    tornadio2.polling
    ~~~~~~~~~~~~~~~~~

    This module implements socket.io polling transports.

    :copyright: (c) 2011 by the Serge S. Koval, see AUTHORS for more details.
    :license: Apache, see LICENSE for more details.
"""
import time
import logging
import urllib

from tornado.web import HTTPError, asynchronous

from tornadio2 import proto, preflight


class TornadioPollingHandlerBase(preflight.PreflightHandler):
    def initialize(self, server):
        self.server = server
        self.session = None

        logging.debug('Initializing %s transport.' % self.name)

    def _get_session(self, session_id):
        # Get session
        session = self.server.get_session(session_id)

        # If session was not found, ignore it
        if session is None:
            raise HTTPError(401)

        # If session is closed, but there are some pending messages left - make sure to send them
        if session.is_closed and not session.send_queue:
            raise HTTPError(401)

        return session

    def _detach(self):
        if self.session:
            self.session.stop_heartbeat()
            self.session.remove_handler(self)

            self.session = None

    @asynchronous
    def get(self, session_id):
        """Default GET handler."""
        raise NotImplementedError()

    @asynchronous
    def post(self, session_id):
        # Get session
        self.session = self._get_session(session_id)

        # Can not send messages to closed session or if preflight() failed
        if self.session.is_closed or not self.preflight():
            raise HTTPError(401)

        data = self.request.body

        # IE XDomainRequest support
        if data.startswith('data='):
            data = data[5:]

        # Process packets one by one
        packets = proto.decode_frames(data)
        for p in packets:
            try:
                self.session.raw_message(p)
            except Exception:
                # Close session if something went wrong
                self.session.close()

        self.set_header('Content-Type', 'text/plain; charset=UTF-8')
        #self.write('')
        self.finish()

    def send_messages(self, messages):
        """Called by the session when some data is available"""
        raise NotImplementedError()

    def session_closed(self):
        """Close associated connection"""
        self._detach()

    def on_connection_close(self):
        self._detach()


class TornadioXHRPollingHandler(TornadioPollingHandlerBase):
    # Transport name
    name = 'xhr-polling'

    def initialize(self, server):
        super(TornadioXHRPollingHandler, self).initialize(server)

        self._timeout = None
        self._timeout_interval = self.server.settings['xhr_polling_timeout']

    @asynchronous
    def get(self, session_id):
        # Get session
        self.session = self._get_session(session_id)

        if not self.session.set_handler(self):
            # TODO: Error logging
            raise HTTPError(401)

        if not self.session.send_queue:
            self._bump_timeout()
        else:
            self.session.flush()

    def _stop_timeout(self):
        if self._timeout is not None:
            self.server.io_loop.remove_timeout(self._timeout)
            self._timeout = None

    def _bump_timeout(self):
        self._stop_timeout()

        self._timeout = self.server.io_loop.add_timeout(
                                time.time() + self._timeout_interval,
                                self._polling_timeout
                                )

    def _polling_timeout(self):
        try:
            self.send_messages([proto.noop()])
        except Exception:
            logging.debug('Exception', exc_info=True)
        finally:
            self._detach()

    def _detach(self):
        self._stop_timeout()

        super(TornadioXHRPollingHandler, self)._detach()

    def send_messages(self, messages):
        # Encode multiple messages as UTF-8 string
        data = proto.encode_frames(messages)

        # Send data to client
        self.preflight()
        self.set_header('Content-Type', 'text/plain; charset=UTF-8')
        self.set_header('Content-Length', len(data))
        self.write(data)

        # Detach connection from session
        self._detach()

        # Close connection
        self.finish()

    def session_closed(self):
        try:
            self.finish()
        except Exception:
            logging.debug('Exception', exc_info=True)
        finally:
            self._detach()


class TornadioHtmlFileHandler(TornadioPollingHandlerBase):
    """IE HtmlFile protocol implementation.

    Uses hidden frame to stream data from the server in one connection.

    Unfortunately, it is unknown if this transport works, as socket.io
    client-side fails in IE7/8.
    """
    # Transport name
    name = 'htmlfile'

    @asynchronous
    def get(self, session_id):
        # Get session
        self.session = self._get_session(session_id)

        if not self.session.set_handler(self):
            raise HTTPError(401)

        self.set_header('Content-Type', 'text/html; charset=UTF-8')
        self.set_header('Connection', 'keep-alive')
        self.write('<html><body><script>var _ = function (msg) { parent.s._(msg, document); };</script>' + (' ' * 174))
        self.flush()

        # Dump any queued messages
        self.session.flush()

        # We need heartbeats
        self.session.reset_heartbeat()

    def send_messages(self, messages):
        data = proto.encode_frames(messages)

        self.write(
            '<script>_(%s);</script>' % proto.json_dumps(data)
            )
        self.flush()

        self.session.delay_heartbeat()

    def session_closed(self):
        try:
            self.finish()
        except Exception:
            logging.debug('Exception', exc_info=True)
        finally:
            self._detach()


class TornadioJSONPHandler(TornadioXHRPollingHandler):
    # Transport name
    name = 'jsonp'

    def initialize(self, server):
        self._index = None

        super(TornadioJSONPHandler, self).initialize(server)

    @asynchronous
    def get(self, session_id):
        self._index = self.get_argument('i', 0)

        super(TornadioJSONPHandler, self).get(session_id)

    @asynchronous
    def post(self, session_id):
        # Get session
        self.session = self._get_session(session_id)

        # Can not send messages to closed session or if preflight() failed
        if self.session.is_closed or not self.preflight():
            raise HTTPError(401)

        data = self.request.body

        # IE XDomainRequest support
        if not data.startswith('d='):
            logging.error('Malformed JSONP POST request')
            raise HTTPError(403)

        data = urllib.unquote(data[2:])

        # Process packets one by one
        packets = proto.decode_frames(data)
        for p in packets:
            try:
                self.session.raw_message(p)
            except Exception:
                # Close session if something went wrong
                self.session.close()

        self.set_header('Content-Type', 'text/plain; charset=UTF-8')
        self.finish()

    def send_messages(self, messages):
        if self._index is None:
            raise HTTPError(401)

        data = proto.encode_frames(messages)

        message = 'io.j[%s](%s);' % (
            self._index,
            proto.json_dumps(data)
            )

        self.preflight()
        self.set_header('Content-Type', 'text/javascript; charset=UTF-8')
        self.set_header('Content-Length', len(message))
        self.set_header('X-XSS-Protection', '0')
        self.set_header('Connection', 'Keep-Alive')
        self.write(message)

        self._detach()

        self.finish()
