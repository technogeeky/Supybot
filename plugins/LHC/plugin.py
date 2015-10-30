###
# Copyright (c) 2002-2004, Jeremiah Fincher
# Copyright (c) 2008-2010, James McCoy
# All rights reserved.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are met:
#
#   * Redistributions of source code must retain the above copyright notice,
#     this list of conditions, and the following disclaimer.
#   * Redistributions in binary form must reproduce the above copyright notice,
#     this list of conditions, and the following disclaimer in the
#     documentation and/or other materials provided with the distribution.
#   * Neither the name of the author of this software nor the name of
#     contributors to this software may be used to endorse or promote products
#     derived from this software without specific prior written consent.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
# AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
# IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE
# ARE DISCLAIMED.  IN NO EVENT SHALL THE COPYRIGHT OWNER OR CONTRIBUTORS BE
# LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR
# CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF
# SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS
# INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN
# CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE)
# ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
# POSSIBILITY OF SUCH DAMAGE.
###

import new
import time
import socket
import sgmllib
import threading
import feedparser
import random

import supybot.conf as conf
import supybot.utils as utils
import supybot.world as world
from supybot.commands import *
import supybot.ircutils as ircutils
import supybot.registry as registry
import supybot.callbacks as callbacks

def getFeedName(irc, msg, args, state):
    if not registry.isValidRegistryName(args[0]):
        state.errorInvalid('feed name', args[0],
                           'Feed names must not include spaces.')
    state.args.append(callbacks.canonicalName(args.pop(0)))
addConverter('feedName', getFeedName)

class LHC(callbacks.Plugin):
    """This plugin is useful both for announcing updates to RSS feeds in a
    channel, and for retrieving the headlines of RSS feeds via command.  Use
    the "add" command to add feeds to this plugin, and use the "announce"
    command to determine what feeds should be announced in a given channel."""
    threaded = True
    def __init__(self, irc):
        self.__parent = super(LHC, self)
        self.__parent.__init__(irc)
        # Schema is feed : [url, command]
        self.feedNames = callbacks.CanonicalNameDict()
        self.locks = {}
        self.lastRequest = {}
        self.cachedFeeds = {}
        self.gettingLockLock = threading.Lock()
        for name in self.registryValue('feeds'):
            self._registerFeed(name)
            try:
                url = self.registryValue(registry.join(['feeds', name]))
            except registry.NonExistentRegistryEntry:
                self.log.warning('%s is not a registered feed, removing.',name)
                continue
            self.makeFeedCommand(name, url)
            self.getFeed('http://lblogbook.cern.ch/Shift/elog.rdf') # So announced feeds don't announce on startup.

    def isCommandMethod(self, name):
        if not self.__parent.isCommandMethod(name):
            if name in self.feedNames:
                return True
            else:
                return False
        else:
            return True

    def listCommands(self):
        return self.__parent.listCommands(self.feedNames.keys())

    def getCommandMethod(self, command):
        try:
            return self.__parent.getCommandMethod(command)
        except AttributeError:
            return self.feedNames[command[0]][1]

    def _registerFeed(self, name, url=''):
        self.registryValue('feeds').add(name)
        group = self.registryValue('feeds', value=False)
        conf.registerGlobalValue(group, name, registry.String(url, ''))

    def __call__(self, irc, msg):
        self.__parent.__call__(irc, msg)
        irc = callbacks.SimpleProxy(irc, msg)
        newFeeds = {}
        for channel in irc.state.channels:
            feeds = self.registryValue('announce', channel)
            for name in feeds:
                commandName = callbacks.canonicalName(name)
                if self.isCommandMethod(commandName):
                    url = self.feedNames[commandName][0]
                else:
                    url = name
                if self.willGetNewFeed(url):
                    newFeeds.setdefault((url, name), []).append(channel)
        for ((url, name), channels) in newFeeds.iteritems():
            # We check if we can acquire the lock right here because if we
            # don't, we'll possibly end up spawning a lot of threads to get
            # the feed, because this thread may run for a number of bytecodes
            # before it switches to a thread that'll get the lock in
            # _newHeadlines.
            if self.acquireLock(url, blocking=False):
                try:
                    t = threading.Thread(target=self._newHeadlines,
                                         name=format('Fetching %u', url),
                                         args=(irc, channels, name, url))
                    self.log.info('Checking for announcements at %u', url)
                    world.threadsSpawned += 1
                    t.setDaemon(True)
                    t.start()
                finally:
                    self.releaseLock(url)
                    time.sleep(0.1) # So other threads can run.

    def buildHeadlines(self, headlines, channel):
        newheadlines = []
        for headline in headlines:
            title = headline[0]
            if self.registryValue('bold', channel):
                title = ircutils.bold(headline[0])
            newheadlines.append(format('%s: [%s] %s',
                                       title,
                                       headline[1],
                                       headline[2]))
        return newheadlines

    def _newHeadlines(self, irc, channels, name, url):
        try:
            # We acquire the lock here so there's only one announcement thread
            # in this code at any given time.  Otherwise, several announcement
            # threads will getFeed (all blocking, in turn); then they'll all
            # want to send their news messages to the appropriate channels.
            # Note that we're allowed to acquire this lock twice within the
            # same thread because it's an RLock and not just a normal Lock.
            self.acquireLock(url)
            try:
                oldresults = self.cachedFeeds[url]
                oldheadlines = self.getHeadlines(oldresults)
            except KeyError:
                oldheadlines = []
            newresults = self.getFeed(url)
            newheadlines = self.getHeadlines(newresults)
            if len(newheadlines) == 1:
                s = newheadlines[0][0]
                if s in ('Timeout downloading feed.',
                         'Unable to download feed.'):
                    self.log.debug('%s %u', s, url)
                    return
            def normalize(headline):
                return (tuple(headline[0].lower().split()), headline[1])
            oldheadlines = set(map(normalize, oldheadlines))
            for (i, headline) in enumerate(newheadlines):
                if normalize(headline) in oldheadlines:
                    newheadlines[i] = None
            newheadlines = filter(None, newheadlines) # Removes Nones.
            if newheadlines:
                for channel in channels:
                    bold = self.registryValue('bold', channel)
                    sep = self.registryValue('headlineSeparator', channel)
                    if bold:
                        sep = ircutils.bold(sep)
                    headlines = self.buildHeadlines(newheadlines, channel)
                    irc.replies(headlines, prefixer='', joiner=sep,
                                to=channel, prefixNick=False, private=True)
        finally:
            self.releaseLock(url)

    def willGetNewFeed(self, url):
        now = time.time()
        wait = self.registryValue('waitPeriod')
        if url not in self.lastRequest or now - self.lastRequest[url] > wait:
            return True
        else:
            return False

    def acquireLock(self, url, blocking=True):
        try:
            self.gettingLockLock.acquire()
            try:
                lock = self.locks[url]
            except KeyError:
                lock = threading.RLock()
                self.locks[url] = lock
            return lock.acquire(blocking=blocking)
        finally:
            self.gettingLockLock.release()

    def releaseLock(self, url):
        self.locks[url].release()

    def getFeed(self, url):
        def error(s):
            return {'items': [{'title': s}]}
        try:
            # This is the most obvious place to acquire the lock, because a
            # malicious user could conceivably flood the bot with rss commands
            # and DoS the website in question.
            self.acquireLock(url)
            if self.willGetNewFeed(url):
                results = {}
                try:
                    self.log.debug('Downloading new feed from %u', url)
                    results = feedparser.parse(url)
                    if 'bozo_exception' in results:
                        raise results['bozo_exception']
                except sgmllib.SGMLParseError:
                    self.log.exception('Uncaught exception from feedparser:')
                    raise callbacks.Error, 'Invalid (unparsable) RSS feed.'
                except socket.timeout:
                    return error('Timeout downloading feed.')
                except Exception, e:
                    # These seem mostly harmless.  We'll need reports of a
                    # kind that isn't.
                    self.log.debug('Allowing bozo_exception %r through.', e)
                if results.get('feed', {}) and self.getHeadlines(results):
                    self.cachedFeeds[url] = results
                    self.lastRequest[url] = time.time()
                else:
                    self.log.debug('Not caching results; feed is empty.')
            try:
                return self.cachedFeeds[url]
            except KeyError:
                wait = self.registryValue('waitPeriod')
                # If there's a problem retrieving the feed, we should back off
                # for a little bit before retrying so that there is time for
                # the error to be resolved.
                self.lastRequest[url] = time.time() - .5 * wait
                return error('Unable to download feed.')
        finally:
            self.releaseLock(url)

    def _getConverter(self, feed):
        toText = utils.web.htmlToText
        if 'encoding' in feed:
            def conv(s):
                # encode() first so there implicit encoding doesn't happen in
                # other functions when unicode and bytestring objects are used
                # together
                s = s.encode(feed['encoding'], 'replace')
                s = toText(s).strip()
                return s
            return conv
        else:
            return lambda s: toText(s).strip()

    def getHeadlines(self, feed):
        headlines = []
        conv = self._getConverter(feed)
        for d in feed['items']:
            title = conv(d['title'])
            if 'LHC, Comments' in title or 'LHC, New State' in title:
                thetime = conv(d['published'])
                # Dumb way to deal with daylight savings due to no implementation of %z in time.strptime
                try:
                    thetime = time.strptime(thetime,"%a, %d %b %Y %H:%M:%S +0100")
                except ValueError:
                    thetime = time.strptime(thetime,"%a, %d %b %Y %H:%M:%S +0200")
                timestamp = time.strftime("%H:%M",thetime)
                description = conv(d['description'])
                headlines.append((funkify_title_probably(title), timestamp, description))
        return headlines
        
    ## this should, with a small probability, remind us of the days when
    ##  this task was instead done (sometimes poorly) by OCR
    ##  which replaced Comments: with Corn ments:
    def funkify_title_probably(title):
        if 'LHC, Comments' in title and random.random() < 0.1 
            return 'LHC, Corn ments'
        else
            return title
            
    class announce(callbacks.Commands):
        def list(self, irc, msg, args, channel):
            """[<channel>]

            Returns the list of feeds announced in <channel>.  <channel> is
            only necessary if the message isn't sent in the channel itself.
            """
            announce = conf.supybot.plugins.LHC.announce
            feeds = format('%L', list(announce.get(channel)()))
            irc.reply(feeds or 'I am currently not announcing any feeds.')
        list = wrap(list, ['channel',])

        def add(self, irc, msg, args, channel):
            """[<channel>]

            Adds the feed. <channel> is only necessary if the
            message isn't sent in the channel itself.
            """
            announce = conf.supybot.plugins.LHC.announce
            S = announce.get(channel)()
            S.add('http://lblogbook.cern.ch/Shift/elog.rdf')
            announce.get(channel).setValue(S)
            irc.replySuccess()
        add = wrap(add, [('checkChannelCapability', 'op')])

        def remove(self, irc, msg, args, channel):
            """[<channel>]

            Removes the feed. <channel> is only necessary if the
            message isn't sent in the channel itself.
            """
            announce = conf.supybot.plugins.LHC.announce
            S = announce.get(channel)()
            S.discard('http://lblogbook.cern.ch/Shift/elog.rdf')
            announce.get(channel).setValue(S)
            irc.replySuccess()
        remove = wrap(remove, [('checkChannelCapability', 'op')])

    def last(self, irc, msg, args):
        """

        Gets the last comment or machine status
        """
        url = 'http://lblogbook.cern.ch/Shift/elog.rdf'
        self.log.debug('Fetching %u', url)
        feed = self.getFeed(url)
        if irc.isChannel(msg.args[0]):
            channel = msg.args[0]
        else:
            channel = None
        headlines = self.getHeadlines(feed)
        if not headlines:
            irc.error('Couldn\'t get RSS feed.')
            return
        headlines = self.buildHeadlines(headlines, channel)
        headlines = headlines[:1]
        sep = self.registryValue('headlineSeparator', channel)
        if self.registryValue('bold', channel):
            sep = ircutils.bold(sep)
        irc.replies(headlines, joiner=sep)
    last = wrap(last)

Class = LHC

# vim:set shiftwidth=4 softtabstop=4 expandtab textwidth=79:
