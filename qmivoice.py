import re
import logging
import subprocess
import contextlib


CID_PATTERN = re.compile(rb'.*\sCID\:\s\'(\d+)\'.*', re.MULTILINE | re.DOTALL)
logger = logging.getLogger('QmiVoice')

CID_RELEASE_RANGE = range(2, 4)


class QmiVoiceException(Exception):
    pass


class QmiVoice:
    '''
    Wraps the qmicli utility by parsing its output
    '''
    def __init__(self, device):
        self._device = device

    def _release_cid(self, cid):
        proc = subprocess.run(
            ['qmicli', '-d', self._device, '--client-cid', str(cid), '--voice-noop']
        )

    @contextlib.contextmanager
    def alloc_cid(self):
        # HACK: Release a few voice CIDs in case they were somehow allocated prior...
        for cid in CID_RELEASE_RANGE:
            self._release_cid(cid)

        proc = subprocess.run(
            ['qmicli', '-d', self._device, '--client-no-release-cid', '--voice-noop'],
            check=True, capture_output=True
        )

        match = re.match(CID_PATTERN, proc.stdout)
        if not match:
            raise QmiVoiceException(proc.stdout)
        cid = match.groups()[0]
        logger.info('QMI allocated voice CID: %s' % (cid,))

        try:
            yield
        finally:
            self._release_cid(cid)
            logger.info('QMI released voice CID: %s' % (cid,))

