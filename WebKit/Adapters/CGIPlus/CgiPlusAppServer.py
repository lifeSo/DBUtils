#!/usr/bin/env python
"""
AppServer

The WebKit app server is a WASD CgiPLus server that accepts requests, hands
them off to the Application and sends the request back over the connection.

The fact that the app server stays resident is what makes it so much quicker
than traditional CGI programming. Everything gets cached.

"""

from Common import *
import AppServer as AppServerModule
from AutoReloadingAppServer import AutoReloadingAppServer as AppServer
from MiscUtils.Funcs import timestamp
from marshal import dumps, loads
import os, sys
from threading import Lock, Thread, Event
import threading
import Queue
import select
import socket
import threading
import time
import errno
import traceback
from WebUtils import Funcs
try:
	from cStringIO import StringIO
except ImportError:
	from StringIO import StringIO

debug = 0

DefaultConfig = {
	'MaxServerThreads':        1,
	'MinServerThreads':        1,
	'StartServerThreads':      1,
}

# Need to know this value for communications
# Note that this limits the size of the dictionary we receive from the AppServer to 2,147,483,647 bytes
intLength = len(dumps(int(1)))

server = None


class CgiPlusAppServer(AppServer):

	## Init ##

	def __init__(self, path=None):
		AppServer.__init__(self, path)
		self._requestID = 1
		self.recordPID()
		self._wasd_running = None

		# temporaire
		from WebKit import Profiler
		Profiler.startTime = time.time()
		self.readyForRequests()

	def addInputHandler(self, handlerClass):
		self._handler = handlerClass

	def isPersistent(self):
		return 0

	def recordPID(self):
		"""Currently do nothing."""
		return

	def initiateShutdown(self):
		self._wasd_running = False
		AppServer.initiateShutdown(self)

	def mainloop(self, timeout=1):
		import wasd
		wasd.init()
		stderr_ini = sys.stderr
		sys.stderr = StringIO()
		self._wasd_running = True
		environ_ini = os.environ
		while 1:
			if not self.running or not self._wasd_running:
				return
			# init environment cgi variables
			os.environ = environ_ini.copy()
			wasd.init_environ()
			print >>sys.__stdout__, "Script-Control: X-stream-mode"
			self._requestID += 1
			self._app._sessions.cleanStaleSessions()
			self.handler = handler = self._handler(self)
			handler.activate(self._requestID)
			handler.handleRequest()
			self.restartIfNecessary()
			self.handler = None
			sys.__stdout__.flush()
			if not self.running or not self._wasd_running:
				return
			# when we want to exit don't send the eof, so
			# WASD don't try to send the next request to the server
			wasd.cgi_eof()
			sys.stderr.close()
			# block until next request
			wasd.cgi_info("")
			sys.stderr = StringIO()

	def shutDown(self):
		self.running=0
		self._shuttingdown=1  #jsl-is this used anywhere?
		print "CgiPlusAppServer: Shutting Down"
		AppServer.shutDown(self)


class Handler:

	def __init__(self, server):
		self._server = server

	def activate(self, requestID):
		"""Activates the handler for processing the request.

		Number is the number of the request, mostly used to identify
		verbose output. Each request should be given a unique,
		incremental number.

		"""
		self._requestID = requestID

	def close(self):
		pass

	def handleRequest(self):
		pass

	def receiveDict(self):
		"""Utility function to receive a marshalled dictionary."""
		pass


class MonitorHandler(Handler):

	protcolName = 'monitor'

	def handleRequest(self):
		verbose = self.server._verbose
		startTime = time.time()
		if verbose:
			print 'BEGIN REQUEST'
			print time.asctime(time.localtime(startTime))
		conn = self._sock
		if verbose:
			print 'receiving request from', conn
		BUFSIZE = 8*1024
		dict = self.receiveDict()
		if dict['format'] == "STATUS":
			conn.send(str(self.server._reqCount))
		if dict['format'] == 'QUIT':
			conn.send("OK")
			conn.close()
			self.server.shutDown()


from WebKit.ASStreamOut import ASStreamOut
class CPASStreamOut(ASStreamOut):
	"""Response stream for CgiPLusAppServer.

	The `CPASASStreamOut` class streams to a given file, so that when `flush`
	is called and the buffer is ready to be written, it sends the data from the
	buffer out on the file. This is the response stream used for requests
	generated by CgiPlusAppServer.

	CP stands for CgiPlusAppServer

	"""

	def __init__(self, file):
		ASStreamOut.__init__(self)
		self._file = file

	def flush(self):
		result = ASStreamOut.flush(self)
		if result: ##a true return value means we can send
			reslen = len(self._buffer)
			self._file.write(self._buffer)
			self._file.flush()
			sent = reslen
			self.pop(sent)


class AdapterHandler(Handler):

	protocolName = 'address'

	def handleRequest(self):
		verbose = self._server._verbose
		self._startTime = time.time()
		if verbose:
			print '%5i  %s ' % (self._requestID, timestamp()['pretty']),

		data = []
		dict = self.receiveDict()
		if dict and verbose and dict.has_key('environ'):
			requestURI = Funcs.requestURI(dict['environ'])
			print requestURI
		else:
			requestURI = None

		dict['input'] = self.makeInput()
		streamOut = TASASStreamOut(self._sock)
		transaction = self._server._app.dispatchRawRequest(dict, streamOut)
		streamOut.close()

		try:
			self._sock.shutdown(1)
			self._sock.close()
		except:
			pass

		if self._server._verbose:
			duration = '%0.2f secs' % (time.time() - self._startTime)
			duration = string.ljust(duration, 19)
			print '%5i  %s  %s' % (self._requestID, duration, requestURI)
			print

		transaction._application=None
		transaction.die()
		del transaction

	def makeInput(self):
		return self._sock.makefile("rb",8012)


# Set to False in DebugAppServer so Python debuggers can trap exceptions
doesRunHandleExceptions = True

class RestartAppServerError(Exception):
	"""Raised by DebugAppServer when needed."""
	pass


def run(workDir=None):
	global server
	from WebKit.CgiPlusServer import CgiPlusAppServerHandler
	runAgain = True
	while runAgain:  # looping in support of RestartAppServerError
		try:
			try:
				runAgain = False
				server = None
				server = CgiPlusAppServer(workDir)
				server.addInputHandler(CgiPlusAppServerHandler)
				try:
					server.mainloop()
				except KeyboardInterrupt, e:
					server.shutDown()
			except RestartAppServerError:
				print
				print "Restarting app server:"
				sys.stdout.flush()
				runAgain = True
			except Exception, e:
				if not doesRunHandleExceptions:
					raise
				if not isinstance(e, SystemExit):
					import traceback
					traceback.print_exc(file=sys.stderr)
				print
				print "Exiting AppServer"
				if server:
					if server.running:
						server.initiateShutdown()
				# if we're here as a result of exit() being called,
				# exit with that return code.
				if isinstance(e,SystemExit):
					sys.exit(e)
		finally:
			AppServerModule.globalAppServer = None
	sys.exit()


def shutDown(arg1,arg2):
	global server
	print "Shutdown Called", time.asctime(time.localtime(time.time()))
	if server:
		server.initiateShutdown()
	else:
		print 'WARNING: No server reference to shutdown.'

import signal
signal.signal(signal.SIGINT, shutDown)
signal.signal(signal.SIGTERM, shutDown)

usage = """
The AppServer is the main process of WebKit.  It handles requests for
servlets from webservers.  ThreadedAppServer takes the following
command line arguments: stop: Stop the currently running Apperver.
daemon: run as a daemon If AppServer is called with no arguments, it
will start the AppServer and record the pid of the process in
appserverpid.txt
"""

import re
settingRE = re.compile(r'^--([a-zA-Z][a-zA-Z0-9]*\.[a-zA-Z][a-zA-Z0-9]*)=')
from MiscUtils import Configurable

def main(args):
	function = run
	daemon = 0
	workDir = None
	sys.stdout = StringIO()
	for i in args[:]:
		if settingRE.match(i):
			match = settingRE.match(i)
			name = match.group(1)
			value = i[match.end():]
			Configurable.addCommandLineSetting(name, value)
		elif i == "stop":
			import AppServer
			function=AppServer.stop
		elif i == "daemon":
			daemon=1
		elif i == "start":
			pass
		elif i[:8] == "workdir=":
			workDir = i[8:]
		else:
			print usage

	function(workDir=workDir)

main([])
