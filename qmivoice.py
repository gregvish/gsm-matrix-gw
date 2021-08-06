import re
import logging
import subprocess
import contextlib


CID_PATTERN = re.compile(rb'.*\sCID\:\s\'(\d+)\'.*', re.MULTILINE | re.DOTALL)
logger = logging.getLogger('QmiVoice')


class QmiVoiceException(Exception):
    pass


class QmiVoice:
    '''
    Wraps the qmicli utility by parsing its output
    '''
    def __init__(self, device):
        self._device = device

    @contextlib.contextmanager
    def alloc_cid(self):
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
            proc = subprocess.run(
                ['qmicli', '-d', self._device, '--client-cid', cid, '--voice-noop'],
                check=True
            )
            logger.info('QMI released voice CID: %s' % (cid,))
