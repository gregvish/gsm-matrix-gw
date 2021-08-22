import re
import os
import asyncio
import logging
import argparse

import serial_asyncio


MODEM_BAUD = 115200
AT_SHORT_TIMEOUT = 0.2
AT_MEDIUM_TIMEOUT = 0.5
AT_LONG_TIMEOUT = 5

logger = logging.getLogger('QuectelModem')


class AtCommandError(Exception):
    pass
class AtStateError(Exception):
    pass


class QuectelModemManager:
    def __init__(self, call_forwarder, sms_forwarder, modem_tty, modem_baud=MODEM_BAUD,
                 sim_card_pin=None):
        self._call_fwd = call_forwarder
        self._sms_fwd = sms_forwarder
        self._modem_tty = modem_tty
        self._modem_baud = modem_baud
        self._sim_card_pin = sim_card_pin

        self._last_cmd = b''
        self._response_q = asyncio.Queue()
        self._urc_q = asyncio.Queue()
        self._in_call = False
        self._call_fwd_task = None

    async def _reset_at(self):
        self._modem_w.write(b'\rATE\r')
        await asyncio.sleep(AT_MEDIUM_TIMEOUT)
        # Cleanout buffer
        while True:
            try:
                await asyncio.wait_for(self._modem_r.read(1), timeout=AT_MEDIUM_TIMEOUT)
            except asyncio.exceptions.TimeoutError:
                break

    async def _tty_rx_handler(self):
        async def getline(timeout=None):
            rx = await asyncio.wait_for(self._modem_r.readline(), timeout=timeout)
            return rx.strip()

        while True:
            line = await getline()

            # If the line isn't the echo of _last_cmd, treat it as a URC
            if not line.startswith(self._last_cmd) and line != b'':
                await self._urc_q.put(line.decode())
                continue

            elif line == b'':
                continue

            # Treat the line as the start of the response to _last_cmd
            lines = []
            while True:
                try:
                    # Append lines until there is a short RX timeout, or OK/ERROR
                    while True:
                        lines.append(await getline(timeout=AT_SHORT_TIMEOUT))
                        if lines[-1] in (b'OK', b'ERROR'):
                            break
                except asyncio.exceptions.TimeoutError:
                    pass

                # Try to send a new AT command to probe if last command finished
                self._modem_w.write(b'AT\r')
                line = await getline(timeout=AT_SHORT_TIMEOUT)

                # If we got AT back, get the OK too and finish
                if line == b'AT':
                    line = await getline(timeout=AT_SHORT_TIMEOUT)
                    if line == b'OK':
                        break
                    else:
                        line.append(line)
                else:
                    # Otherwise, this line is part of the response. Continue
                    lines.append(line)

            await self._response_q.put((b'\n'.join(lines)).decode())


    async def do_cmd(self, cmd, timeout=AT_LONG_TIMEOUT):
        self._last_cmd = cmd.encode()
        self._modem_w.write(b'%s\r' % (self._last_cmd,))
        result = await asyncio.wait_for(self._response_q.get(), timeout=timeout)
        logger.debug('%s -> %r' % (cmd, result))
        return result

    def verify_ok(self, result):
        if not result.endswith('OK'):
            raise AtCommandError(result)

    async def _sim_unlock(self):
        if not self._sim_card_pin:
            raise AtStateError('SIM unlock needed but not PIN setup')

        pin_counters = await self.do_cmd('AT+QPINC?')
        left, total = re.match(r'.*\"SC\",(\d+),(\d+)', pin_counters).groups()

        if int(total) - int(left) > 1:
            raise AtStateError('SIM unlock attempts not perfect %s/%s' % (left, total))

        self.verify_ok(await self.do_cmd('AT+CPIN=%s' % (self._sim_card_pin,)))

    async def _reset(self):
        self.verify_ok(await self.do_cmd('AT'))
        self.verify_ok(await self.do_cmd('AT+QURCCFG="urcport","all"'))
        self.verify_ok(await self.do_cmd('ATH0'))
        self.verify_ok(await self.do_cmd('AT+CFUN=0'))
        self.verify_ok(await self.do_cmd('AT+CFUN=1'))

        while True:
            urc = await asyncio.wait_for(self._urc_q.get(), timeout=AT_LONG_TIMEOUT)
            logger.info('URC -> %r' % (urc,))

            if '+CPIN: SIM PIN' in urc:
                await self._sim_unlock()

            elif 'PB DONE' in urc:
                break

        self.verify_ok(await self.do_cmd('AT+CMGF=1'))
        logger.info('%r' % (await self.do_cmd('AT+COPS?'),))

    async def _handle_call(self):
        result = await self.do_cmd('AT+CLCC')

        for call in [c for c in result.split('\n') if c.startswith('+CLCC')]:
            call = call[len('+CLCC: '):]
            idx, dir, state, mode, multiparty, number, type = call.split(',')
            # Make sure it's a Voice call, Mobile Terminated and Incoming state
            if mode == '0' and dir == '1' and state == '4':
                break
        else:
            logger.warning('Tried to handle a bad call: %r' % ((mode, dir, state, number),))
            return

        self._in_call = True
        number = number.replace('"', '')
        logger.info('Got call! #%s, number: %s, type: %s' % (idx, number, type))

        async def call_ended_cb():
            self._in_call = False
            self._call_fwd_task = None
            logger.info('Call disconnected. Sending ATH0!')
            self.verify_ok(await self.do_cmd('ATH0'))

        async def call_connected_cb():
            logger.info('Call connected. Sending ATA!')
            self.verify_ok(await self.do_cmd('ATA'))

        self._call_fwd_task = self._call_fwd(
            'GSM %s' % (number,), call_connected_cb, call_ended_cb
        ).run()

    async def _handle_sms(self):
        result = await self.do_cmd('AT+CMGL')
        self.verify_ok(result)
        result = result.replace('\nOK', '')
        await self._sms_fwd(result).send()

    async def _urc_handler(self):
        while True:
            urc = await self._urc_q.get()
            logger.info('URC -> %r' % (urc,))

            if 'RING' == urc and not self._in_call:
                await self._handle_call()

            if 'NO CARRIER' in urc and self._in_call:
                logger.info('Got GSM hangup. Cancelling call task!')
                self._call_fwd_task.cancel()

            elif '+CMTI:' in urc:
                await self._handle_sms()

            elif '+CPIN: NOT READY' in urc:
                raise AtStateError(urc)

            else:
                logger.warning('Uhandled URC: %r' % (urc,))

    async def run(self):
        self._modem_r, self._modem_w = await serial_asyncio.open_serial_connection(
            url=self._modem_tty, baudrate=self._modem_baud
        )

        await self._reset_at()
        rx_task = asyncio.create_task(self._tty_rx_handler())

        logger.info('Got AT shell to modem. Resetting')
        await self._reset()
        urc_task = asyncio.create_task(self._urc_handler())

        await asyncio.gather(rx_task, urc_task)

