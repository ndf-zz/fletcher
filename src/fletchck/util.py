# SPDX-License-Identifier: MIT
"""Application support utilities"""

import os
import sys
import json
import struct
import math
from secrets import randbits, token_hex
from passlib.hash import argon2 as kdf
from tempfile import NamedTemporaryFile, mkdtemp
from logging import getLogger, Handler, DEBUG, INFO, WARNING
from subprocess import run
from . import action
from . import check
from . import defaults
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from apscheduler.triggers.cron import CronTrigger

_log = getLogger('fletchck.util')
_log.setLevel(DEBUG)
getLogger('apscheduler.executors').setLevel(WARNING)
getLogger('apscheduler.executors.default').setLevel(WARNING)

_INTTRIGKEYS = {'weeks', 'days', 'hours', 'minutes', 'seconds', 'jitter'}
_INTERVALKEYS = {
    'weeks': 'week',
    'days': 'day',
    'hours': 'hr',
    'minutes': 'min',
    'seconds': 'sec',
    'start_date': 'start',
    'end_date': 'end',
    'timezone': 'z',
    'jitter': 'delay'
}
_CRONKEYS = {
    'year': 'year',
    'month': 'month',
    'day': 'day',
    'week': 'week',
    'day_of_week': 'weekday',
    'hour': 'hr',
    'minute': 'min',
    'second': 'sec',
    'start_date': 'start',
    'end_date': 'end',
    'timezone': 'z',
    'jitter': 'delay',
}


class SaveFile():
    """Tempfile-backed save file contextmanager.

       Creates a temporary file with the desired mode and encoding
       and returns a context manager and writable file handle.

       On close, the temp file is atomically moved to the provided
       filename (if possible).
    """

    def __init__(self,
                 filename,
                 mode='t',
                 encoding='utf-8',
                 tempdir='.',
                 perm=0o600):
        self.__sfile = filename
        self.__path = tempdir
        self.__perm = perm
        if mode == 'b':
            encoding = None
        self.__tfile = NamedTemporaryFile(mode='w' + mode,
                                          suffix='.tmp',
                                          prefix='sav_',
                                          dir=self.__path,
                                          encoding=encoding,
                                          delete=False)

    def __enter__(self):
        return self.__tfile

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.__tfile.close()
        if exc_type is not None:
            return False  # raise exception
        # otherwise, file is saved ok in temp file
        os.chmod(self.__tfile.name, self.__perm)
        os.rename(self.__tfile.name, self.__sfile)
        return True


class LogHandler(Handler):

    def __init__(self, site):
        self.site = site
        Handler.__init__(self)

    def emit(self, record):
        """Append record to log and prune early entries"""
        msg = self.format(record)
        self.site.log.append(msg)
        if len(self.site.log) > 200:
            del (self.site.log[0:10])


def trigger2Text(trigger):
    """Convert a trigger schedule object to text string"""
    rv = []
    if isinstance(trigger, dict):
        if 'interval' in trigger:
            rv.append('interval')
            for key in _INTERVALKEYS:
                if key in trigger['interval']:
                    rv.append(str(trigger['interval'][key]))
                    rv.append(_INTERVALKEYS[key])
        elif 'cron' in trigger:
            rv.append('cron')
            for key in _CRONKEYS:
                if key in trigger['cron']:
                    rv.append(str(trigger['cron'][key]))
                    rv.append(_CRONKEYS[key])
    return ' '.join(rv)


def text2Trigger(triggerText):
    """Read, validate and return trigger definition"""
    ret = None
    try:
        trigger = None
        if triggerText:
            tv = triggerText.lower().split()
            _log.debug('tv is: %r', tv)
            if tv:
                # check type prefix
                trigType = tv[0]
                if trigType == 'interval':
                    tv.pop(0)
                    trigger = {'interval': {}}
                elif trigType == 'cron':
                    tv.pop(0)
                    trigger = {'cron': {}}
                else:
                    _log.debug('Assuming interval')
                    trigger = {'interval': {}}

                keyMap = {}
                trigMap = None
                if 'interval' in trigger:
                    trigMap = trigger['interval']
                    for k in _INTERVALKEYS:
                        keyMap[k] = k
                        keyMap[_INTERVALKEYS[k]] = k
                elif 'cron' in trigger:
                    trigMap = trigger['cron']
                    for k in _CRONKEYS:
                        keyMap[k] = k
                        keyMap[_CRONKEYS[k]] = k

                # scan input text
                nextVal = []
                while tv:
                    # check for value:
                    if tv[0] in keyMap:
                        _log.debug('Ignoring spurious unit %s', tv[0])
                        tv.pop(0)
                        continue

                    nextVal.append(tv.pop(0))
                    if tv:
                        if tv[0] in keyMap:
                            unit = tv.pop(0)
                            val = ' '.join(nextVal)
                            key = keyMap[unit]
                            if key in _INTTRIGKEYS:
                                val = int(val)
                            if key in trigMap:
                                _log.debug('Trigger key %s re-defined', key)
                            trigMap[key] = val
                            nextVal = []
                if nextVal:
                    # Lazily assume minutes for degenerate input
                    val = ' '.join(nextVal)
                    _log.debug(
                        'Extra value without units %s, assuming minutes', val)
                    key = keyMap['min']
                    if key in _INTTRIGKEYS:
                        val = int(val)
                    if key in trigMap:
                        _log.debug('Trigger key %s re-defined', key)
                    trigMap[key] = val
                    nextVal = []

                # !! TEMP !!
                _log.debug('trigger is now: %r', trigger)

                # try and create a trigger from the definition
                if 'interval' in trigger:
                    t = IntervalTrigger(**trigMap)
                elif 'cron' in trigger:
                    t = CronTrigger(**trigMap)

                ret = trigger
    except Exception as e:
        _log.info('Invalid trigger %s: %s', e.__class__.__name__, e)
    return ret


def checkPass(pw, hash):
    return kdf.verify(pw, hash[0:1024])


def createHash(pw):
    return kdf.using(rounds=defaults.PASSROUNDS).hash(pw)


def randPass():
    """Return a random passkey"""
    choiceLen = len(defaults.PASSCHARS)
    if choiceLen < 8:
        raise RuntimeError('Unexpected length passchars')
    depth = int(math.floor(math.log2(choiceLen)))
    clen = 2**depth
    if clen != choiceLen:
        _log.warning('Using first %r chars of passchars', clen)
    passLen = int(math.ceil(defaults.PASSBITS / depth))
    rawBits = randbits(passLen * depth)
    mask = clen - 1
    pv = []
    for i in range(0, passLen):
        pv.append(defaults.PASSCHARS[rawBits & mask])
        rawBits >>= depth
    return ''.join(pv)


def saveSite(site):
    """Save the current site state to disk"""
    dstCfg = {'base': site.base, 'webui': None}
    if site.webCfg is not None:
        dstCfg['webui'] = {}
        for k in defaults.WEBUICONFIG:
            dstCfg['webui'][k] = site.webCfg[k]
    dstCfg['actions'] = {}
    for a in site.actions:
        dstCfg['actions'][a] = site.actions[a].flatten()
    dstCfg['checks'] = {}
    for c in site.checks:
        dstCfg['checks'][c] = site.checks[c].flatten()
    dstCfg['log'] = site.log

    # backup existing config and save
    tmpName = None
    if os.path.exists(site.configFile):
        tmpName = site.configFile + token_hex(6)
        os.link(site.configFile, tmpName)
    with SaveFile(site.configFile) as f:
        json.dump(dstCfg, f, indent=1)
    if tmpName is not None:
        os.rename(tmpName, site.configFile + '.bak')
    _log.debug('Saved site config to %r', site.configFile)


def initSite(path, webUi=True):
    """Prepare a new empty site under path, returns True to continue"""
    if not sys.stdin.isatty():
        _log.error('Init requires user input - exiting')
        return False

    cfgPath = os.path.realpath(path)
    cfgFile = os.path.join(cfgPath, defaults.CONFIGPATH)
    backup = False

    # check for an existing config
    if os.path.exists(cfgFile):
        prompt = 'Replace existing site? (y/N) '
        choice = input(prompt)
        if not choice or choice.lower()[0] != 'y':
            _log.error('Existing site not overwritten')
            return False

    # create initial configuration
    siteCfg = {}
    siteCfg['base'] = cfgPath
    if webUi:
        siteCfg['webui'] = dict(defaults.WEBUICONFIG)
        siteCfg['webui']['port'] = 30000 + randbits(15)
        mkCert(cfgPath, siteCfg['webui']['hostname'])
        siteCfg['webui']['cert'] = os.path.join(cfgPath, defaults.SSLCERT)
        siteCfg['webui']['key'] = os.path.join(cfgPath, defaults.SSLKEY)
        siteCfg['actions'] = {}
        siteCfg['checks'] = {}

        # create admin user
        siteCfg['webui']['users'] = {}
        adminPw = randPass()
        siteCfg['webui']['users']['admin'] = createHash(adminPw)
        # add dummy hash for unknown users
        siteCfg['webui']['users'][''] = createHash(randPass())
    else:
        siteCfg['webui'] = None

    # saveconfig
    tmpName = None
    if os.path.exists(cfgFile):
        tmpName = cfgFile + token_hex(6)
        os.link(cfgFile, tmpName)
    with SaveFile(cfgFile) as f:
        json.dump(siteCfg, f, indent=1)
    if tmpName is not None:
        os.rename(tmpName, cfgFile + '.bak')

    # report
    if webUi:
        print(
            '\nSite address:\thttps://%s:%d\nAdmin password:\t%s\n' %
            (siteCfg['webui']['hostname'], siteCfg['webui']['port'], adminPw))
    else:
        print('\nConfigured without web interface.\n')
    choice = input('Start? (Y/n) ')
    if choice and choice.lower()[0] == 'n':
        return False
    return True


def updateCheck(site, oldName, newName, config):
    """Update an existing check on a running site"""
    # un-schedule
    job = site.scheduler.get_job(oldName)
    if job is not None:
        _log.debug('Removing %s (%r) from schedule', oldName, job)
        site.scheduler.remove_job(oldName)

    # fetch handle to old check and remove from site
    oldCheck = site.checks[oldName]
    del site.checks[oldName]

    # add updated config to site with new name
    addCheck(site, newName, config)

    # repair dependencies and sequences
    newCheck = site.checks[newName]
    for name in site.checks:
        if name != newName:
            c = site.checks[name]
            c.replace_depend(oldName, newCheck)
            if c.checkType == 'sequence':
                c.replace_check(oldName, newCheck)
            if oldName != newName:
                if 'checks' in c.options:
                    if isinstance(c.options['checks'], list):
                        cl = c.options['checks']
                        if oldName in cl:
                            cl[cl.index(oldName)] = newName


def addCheck(site, name, config):
    """Add the named check to running site"""
    newCheck = check.loadCheck(name, config)

    # add actions to check
    if 'actions' in config:
        if isinstance(config['actions'], list):
            for a in config['actions']:
                if a in site.actions:
                    newCheck.add_action(site.actions[a])
                else:
                    _log.info('%s ignored unknown action %s', name, a)

    # update check dependencies
    if 'depends' in config:
        if isinstance(config['depends'], list):
            for d in config['depends']:
                if d in site.checks:
                    newCheck.add_depend(site.checks[d])

    # update sequence checks
    if newCheck.checkType == 'sequence':
        if 'checks' in newCheck.options:
            if isinstance(newCheck.options['checks'], list):
                for s in newCheck.options['checks']:
                    if s in site.checks:
                        newCheck.add_check(site.checks[s])

    # add check to site
    site.checks[name] = newCheck
    _log.debug('Load check %r (%s)', name, newCheck.checkType)

    # schedule check
    if newCheck.trigger is not None:
        trigOpts = {}
        trigType = None
        if 'interval' in newCheck.trigger:
            trigType = 'interval'
            trigOpts = newCheck.trigger['interval']
        elif 'cron' in newCheck.trigger:
            trigType = 'cron'
            trigOpts = newCheck.trigger['cron']
        if trigType is not None:
            _log.debug('Adding %s %s trigger to schedule: %r', name, trigType,
                       trigOpts)
            site.scheduler.add_job(newCheck.update,
                                   trigType,
                                   id=name,
                                   **trigOpts)


def deleteCheck(site, check):
    """Remove check from running site"""
    # un-schedule
    job = site.scheduler.get_job(check)
    if job is not None:
        _log.debug('Removing %s (%r) from schedule', check, job)
        site.scheduler.remove_job(check)

    # remove
    if check in site.checks:
        tempCheck = site.checks[check]
        del site.checks[check]

        # remove check from depends and sequences
        for name in site.checks:
            c = site.checks[name]
            c.del_depend(check)
            if c.checkType == 'sequence':
                c.del_check(check)
            if 'checks' in c.options:
                if isinstance(c.options['checks'], list):
                    if check in c.options['checks']:
                        _log.debug('Removing %s from %s options', check, name)
                        c.options['checks'].remove(check)

    _log.warning('Deleted check %s from site', check)


def loadSite(site):
    """Load and initialise site"""
    cfg = None
    try:
        srcCfg = None
        with open(site.configFile) as f:
            srcCfg = json.load(f)

        if 'base' in srcCfg and isinstance(srcCfg['base'], str):
            site.base = srcCfg['base']

        if 'webui' in srcCfg and isinstance(srcCfg['webui'], dict):
            site.webCfg = {}
            for k in defaults.WEBUICONFIG:
                if k in srcCfg['webui']:
                    site.webCfg[k] = srcCfg['webui'][k]
                else:
                    site.webCfg[k] = defaults.WEBUICONFIG[k]

        if 'log' in srcCfg and isinstance(srcCfg['log'], list):
            site.log = srcCfg['log']

        scheduler = AsyncIOScheduler()

        # load actions
        site.actions = {}
        if 'actions' in srcCfg and isinstance(srcCfg['actions'], dict):
            for a in srcCfg['actions']:
                nAct = action.loadAction(a, srcCfg['actions'][a])
                if nAct is not None:
                    site.actions[a] = nAct
                    _log.debug('Load action %r (%s)', a, nAct.actionType)

        # load checks
        site.checks = {}
        if 'checks' in srcCfg and isinstance(srcCfg['checks'], dict):
            for c in srcCfg['checks']:
                if isinstance(srcCfg['checks'][c], dict):
                    newCheck = check.loadCheck(c, srcCfg['checks'][c])
                    # add actions
                    if 'actions' in srcCfg['checks'][c]:
                        if isinstance(srcCfg['checks'][c]['actions'], list):
                            for a in srcCfg['checks'][c]['actions']:
                                if a in site.actions:
                                    newCheck.add_action(site.actions[a])
                                else:
                                    _log.info('%s ignored unknown action %s',
                                              c, a)
                    site.checks[c] = newCheck
                    _log.debug('Load check %r (%s)', c, newCheck.checkType)
        # patch the check dependencies, sequences and triggers
        for c in site.checks:
            if c in srcCfg['checks'] and 'depends' in srcCfg['checks'][c]:
                if isinstance(srcCfg['checks'][c]['depends'], list):
                    for d in srcCfg['checks'][c]['depends']:
                        if d in site.checks:
                            site.checks[c].add_depend(site.checks[d])
            if site.checks[c].checkType == 'sequence':
                if 'checks' in site.checks[c].options:
                    if isinstance(site.checks[c].options['checks'], list):
                        for s in site.checks[c].options['checks']:
                            if s in site.checks:
                                site.checks[c].add_check(site.checks[s])
            if site.checks[c].trigger is not None:
                trigOpts = {}
                trigType = None
                if 'interval' in site.checks[c].trigger:
                    if isinstance(site.checks[c].trigger['interval'], dict):
                        trigOpts = site.checks[c].trigger['interval']
                    trigType = 'interval'
                elif 'cron' in site.checks[c].trigger:
                    if isinstance(site.checks[c].trigger['cron'], dict):
                        trigOpts = site.checks[c].trigger['cron']
                    trigType = 'cron'
                if trigType is not None:
                    _log.debug('Adding %s trigger to check %s: %r', trigType,
                               c, trigOpts)
                    scheduler.add_job(site.checks[c].update,
                                      trigType,
                                      id=c,
                                      **trigOpts)
                else:
                    _log.info('Invalid trigger for %s ignored', c)
                    site.checks[c].trigger = None

        site.scheduler = scheduler
        site.scheduler.start()
    except Exception as e:
        _log.error('%s reading config: %s', e.__class__.__name__, e)


def mkCert(path, hostname):
    """Call openssl to make a self-signed certificate for hostname"""
    # Consider removal or replacement
    _log.debug('Creating self-signed SSL cert for %r at %r', hostname, path)
    crtTmp = None
    with NamedTemporaryFile(mode='w',
                            suffix='.tmp',
                            prefix='sav_',
                            dir=path,
                            delete=False) as f:
        crtTmp = f.name
    keyTmp = None
    with NamedTemporaryFile(mode='w',
                            suffix='.tmp',
                            prefix='sav_',
                            dir=path,
                            delete=False) as f:
        keyTmp = f.name
    crtOut = os.path.join(path, defaults.SSLCERT)
    keyOut = os.path.join(path, defaults.SSLKEY)
    template = """
[dn]
CN=%s
[req]
distinguished_name = dn
[EXT]
subjectAltName=DNS:%s
keyUsage=digitalSignature
extendedKeyUsage=serverAuth""" % (hostname, hostname)
    subject = '/CN=%s' % (hostname)
    cmd = [
        'openssl', 'req', '-x509', '-out', crtTmp, '-keyout', keyTmp,
        '-newkey', 'rsa:2048', '-nodes', '-sha256', '-subj', subject,
        '-extensions', 'EXT', '-config', '-'
    ]
    try:
        ret = run(cmd, input=template.encode('utf-8'), capture_output=True)
        if ret.returncode != 0:
            _log.error('Error creating SSL certificate: %s', ret.stderr)
        _log.debug('SSL certificate created OK')
        os.rename(crtTmp, crtOut)
        os.rename(keyTmp, keyOut)
    except Exception as e:
        _log.error('Error running openssl')
