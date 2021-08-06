import os
import json
import random
import aiohttp
import asyncio
import logging
import argparse
import functools
import contextlib

import serial_asyncio
from nio import (
    AsyncClient, AsyncClientConfig, LoginResponse, RoomMessageText, BadEvent, Event,
    CallEvent, CallInviteEvent, CallHangupEvent, CallCandidatesEvent, CallAnswerEvent
)
from aiortc import (
    RTCIceCandidate, RTCConfiguration, RTCPeerConnection, RTCSessionDescription
)
from aiortc.contrib.media import MediaPlayer, MediaRecorder

import qmivoice


EXTERNAL_IP_GETTER_URL = 'http://checkip.amazonaws.com'
STORE_DIR = './store'
CREDS_FILE = os.path.join(STORE_DIR, 'creds.json')
ALSA_DEVICE = 'GsmModemCard'
MODEM_BAUD = 115200
AT_SHORT_TIMEOUT = 0.2
AT_MEDIUM_TIMEOUT = 0.5
AT_LONG_TIMEOUT = 5

logger = logging.getLogger('GsmGw')


async def do_matrix_login(homeserver, user, password):
    if not os.path.exists(STORE_DIR):
        os.makedirs(STORE_DIR)
        logger.info('Created store dir')

    client_config = AsyncClientConfig(store_sync_tokens=True,
                                      encryption_enabled=True)

    if not os.path.exists(CREDS_FILE):
        client = AsyncClient(homeserver=homeserver, user=user,
                             store_path=STORE_DIR, config=client_config)
        res = await client.login(password)

        if not (isinstance(res, LoginResponse)):
            logger.error('Login fail.')
            return

        with open(CREDS_FILE, "w") as creds:
            json.dump({
                'device_id': res.device_id,
                'user_id': res.user_id,
                'access_token': res.access_token,
            }, creds)
        logger.info('Login success. Saved creds to %s' % (CREDS_FILE,))
        await client.close()

    logger.info('Using saved creds from %s' % (CREDS_FILE,))
    with open(CREDS_FILE, "r") as creds:
        creds = json.load(creds)
        client = AsyncClient(homeserver=homeserver, user=creds['user_id'],
                             store_path=STORE_DIR, config=client_config,
                             device_id=creds['device_id'])
        client.restore_login(user_id=creds['user_id'],
                             device_id=creds['device_id'],
                             access_token=creds['access_token'])

    if client.should_upload_keys:
        await client.keys_upload()
        logger.info('Uploaded E2EE keys')

    return client


class MatrixEventHandler:
    _call_event_types = ('m.call.invite', 'm.call.answer',
                         'm.call.candidates', 'm.call.hangup')
    _call_event_classes = (CallInviteEvent, CallAnswerEvent,
                           CallCandidatesEvent, CallHangupEvent)

    def __init__(self, client):
        self._client = client
        self._call_events = {x: {} for x in self._call_event_classes}
        self._client.add_event_callback(self._text_msg_cb, RoomMessageText)
        self._client.add_event_callback(self._call_event_cb, CallEvent)
        self._client.add_event_callback(self._bad_event_cb, BadEvent)

    async def _text_msg_cb(self, room, event):
        print('>>> Text: [%s]:(%s) %s' % (
            room.display_name, room.user_name(event.sender), event.body
        ))

    async def _bad_event_cb(self, room, event):
        # BUG: some remote clients send version field as string, against the schema
        if event.source['type'] in self._call_event_types:
            event.source['content']['version'] = int(event.source['content']['version'])
            # Re-parse and check if we fixed it
            event = Event.parse_event(event.source)
            if not isinstance(event, BadEvent):
                return (await self._call_event_cb(room, event))

        logger.warning('!!! Received bad event: %r' % (event,))

    async def _call_event_cb(self, room, event):
        event_type = type(event)
        if event.call_id not in self._call_events[event_type]:
            logger.warning('_call_event_cb called with unknown call_id, type: %s' % (
                event_type,
            ))
            return
        await self._call_events[event_type][event.call_id].put(event)

    def prepare_for_call_id(self, call_id):
        for event_dict in self._call_events.values():
            event_dict[call_id] = asyncio.Queue()

    def discard_for_call_id(self, call_id):
        for event_dict in self._call_events.values():
            if call_id in event_dict:
                del event_dict[call_id]

    async def get_call_event(self, type, call_id):
        return (await self._call_events[type][call_id].get())


class MatrixCallForwarder:
    def __init__(self, matrix_client, matrix_handler, room, default_displayname,
                 udp_port, callerid, connected_cb=None, ended_cb=None, call_timeout=90):
        self._matrix_client = matrix_client
        self._matrix_handler = matrix_handler
        self._room = room
        self._default_displayname = default_displayname
        self._udp_port = udp_port
        self._call_timeout = call_timeout
        self._callerid = callerid
        self._connected_cb = connected_cb
        self._ended_cb = ended_cb
        self._external_ip = asyncio.Future()

    def run(self):
        asyncio.create_task(self._get_external_ip())
        return asyncio.create_task(self._call_with_displayname())

    async def _get_external_ip(self):
        try:
            async with aiohttp.request('GET', EXTERNAL_IP_GETTER_URL) as req:
                ip = (await req.text()).strip()
                self._external_ip.set_result(ip)
                logger.info('Got external IP: %s' % (ip,))
        except Exception as e:
            self._external_ip.set_exception(e)
            raise

    async def _call_with_displayname(self):
        try:
            await self._matrix_client.set_displayname(self._callerid)
            await self._call()
        finally:
            if self._ended_cb:
                await self._ended_cb()
            await self._matrix_client.set_displayname(self._default_displayname)

    def _patch_sdp(self, sdp, external_ip, udp_port):
        new_sdp = bytearray(sdp.encode())

        # Try to find an ICE candidate in the SDP, so that we can add one before it
        idx = new_sdp.lower().find(b'a=candidate:')
        if idx < 0:
            return sdp
        # Find the strange magic number after the 'udp' delimiter in the ICE candidate
        proto = ' udp '
        magic = sdp[sdp.lower().find(proto) + len(proto): ].split(' ')[0]

        new_sdp[idx: idx] = ('a=candidate:%s 1 udp %s %s %d typ host\n' % (
            os.urandom(16).hex(), magic, external_ip, udp_port
        )).encode()

        logger.info('Patched SDP, added ICE candidate')
        return new_sdp.decode()

    async def _call(self):
        logger.info('Starting RTC call')
        # Do not use any STUN/TURN servers (we use manual port forwarding)
        pc = RTCPeerConnection(RTCConfiguration(iceServers=[]))
        player = MediaPlayer(ALSA_DEVICE, format='alsa')
        recorder = MediaRecorder(ALSA_DEVICE, format='alsa')

        @pc.on("track")
        def on_track(track):
            logger.info("Receiving track %s" % (track.kind,))
            recorder.addTrack(track)

        pc.addTrack(player.audio)
        offer = await pc.createOffer()
        await pc.setLocalDescription(offer)
        logger.info('Created offer')

        hangup = False
        call_id = str(random.randint(0, 2**31))
        self._matrix_handler.prepare_for_call_id(call_id)
        logger.info('Call id: %s' % (call_id,))

        await self._matrix_client.room_send(
            self._room,
            'm.call.invite', {
                'call_id': call_id,
                'version': 0,
                'lifetime': self._call_timeout * 1000,
                'offer': {
                    'type': 'offer',
                    'sdp': self._patch_sdp(
                         pc.localDescription.sdp,
                         (await self._external_ip),
                         self._udp_port
                     ),
                },
            },
            ignore_unverified_devices=True
        )

        try:
            call_waiter = asyncio.as_completed((
                self._matrix_handler.get_call_event(CallAnswerEvent, call_id),
                self._matrix_handler.get_call_event(CallHangupEvent, call_id),
            ))
            try:
                answer = await asyncio.wait_for(
                    next(call_waiter), timeout=self._call_timeout
                )
            except asyncio.exceptions.TimeoutError:
                logger.info('Call timed out')
                return

            if not isinstance(answer, CallAnswerEvent):
                logger.info('Call hung up. %r' % (type(answer),))
                return

            await pc.setRemoteDescription(RTCSessionDescription(
                sdp=answer.answer['sdp'], type=answer.answer['type']
            ))
            await recorder.start()

            if self._connected_cb:
                await self._connected_cb()
            logger.info('Call established. Waiting for hangup...')
            await next(call_waiter)
            hangup = True

        finally:
            if not hangup:
                await self._matrix_client.room_send(
                    self._room,
                    'm.call.hangup', {
                        'call_id': call_id,
                        'version': 0,
                    },
                    ignore_unverified_devices=True
                )
            await pc.close()
            await recorder.stop()
            # HACK: not sure why there isn't a public stop method
            player._stop(player.audio)
            self._matrix_handler.discard_for_call_id(call_id)
            logger.info('Call finished.')


def udp_random_port_monkeypatch(constant_port):
    '''
    HACK: Monkeypatch the loop.create_datagram_endpoint method, so that the call in
    aioice/ice.py:get_component_candidates will use a constant UDP port (for port forwarding)
    '''
    old_func = asyncio.get_event_loop().create_datagram_endpoint
    def wrapper(*args, **kwargs):
        if 'local_addr' in kwargs and kwargs['local_addr'][1] == 0:
            logger.info('Monkeypatching random bind port to a constant port')
            kwargs['local_addr'] = (kwargs['local_addr'][0], constant_port)
            kwargs['reuse_port'] = True
        return old_func(*args, **kwargs)
    asyncio.get_event_loop().create_datagram_endpoint = wrapper


class MatrixSmsForwarder:
    def __init__(self, matrix_client, room, msg):
        self._matrix_client = matrix_client
        self._room = room
        self._msg = msg

    async def send(self):
        await self._matrix_client.room_send(
            self._room,
            'm.room.message', {
                'msgtype': 'm.text',
                'body': self._msg,
            },
            ignore_unverified_devices=True
        )

class AtCommandError(Exception):
    pass
class AtStateError(Exception):
    pass


class QuectelModemManager:
    def __init__(self, call_forwarder, sms_forwarder, modem_tty, modem_baud):
        self._call_fwd = call_forwarder
        self._sms_fwd = sms_forwarder
        self._modem_tty = modem_tty
        self._modem_baud = modem_baud

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

    async def _reset(self):
        self.verify_ok(await self.do_cmd('AT'))
        self.verify_ok(await self.do_cmd('AT+QURCCFG="urcport","all"'))
        self.verify_ok(await self.do_cmd('ATH0'))
        self.verify_ok(await self.do_cmd('AT+CFUN=0'))
        self.verify_ok(await self.do_cmd('AT+CFUN=1'))

        while True:
            urc = await asyncio.wait_for(self._urc_q.get(), timeout=AT_LONG_TIMEOUT)
            logger.info('URC -> %r' % (urc,))
            if 'PB DONE' in urc:
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


def parse_cmdline():
    parser = argparse.ArgumentParser(description='GSM to Matrix Gateway bot')
    parser.add_argument('--homeserver', help='Matrix homeserver', required=True)
    parser.add_argument('--user', help='Bots username on homeserver', required=True)
    parser.add_argument('--password', help='Bots password')
    parser.add_argument('--udp_port', help='UDP port for voice (that is port forwarded)',
                        type=int, required=True)
    parser.add_argument('--modem_tty', help='TTY device of the modem for AT', required=True)
    parser.add_argument('--modem_dev', help='Modem device for QMI', required=True)
    parser.add_argument('--call_timeout', help='Timeout for ringing before hangup',
                        type=int, default=90)
    return parser.parse_args()


async def main():
    logging.basicConfig(level=logging.INFO)

    args = parse_cmdline()

    matrix_client = await do_matrix_login(args.homeserver, args.user, args.password)
    logger.info('Logged in.')

    # Do this to sync rooms and discard missed messages
    res = await matrix_client.sync(full_state=True)
    joined_rooms = list(res.rooms.join.keys())
    room = joined_rooms.pop(0)
    logger.info('Using room: %s, other possible rooms are: %r' % (room, joined_rooms))

    matrix_handler = MatrixEventHandler(matrix_client)
    matrix_call_fwd = functools.partial(
        MatrixCallForwarder,
        matrix_client, matrix_handler, room, args.user, args.udp_port,
        call_timeout=args.call_timeout
    )
    matrix_sms_fwd = functools.partial(MatrixSmsForwarder, matrix_client, room)
    modem_manager = QuectelModemManager(
        matrix_call_fwd, matrix_sms_fwd, args.modem_tty, MODEM_BAUD
    )

    udp_random_port_monkeypatch(args.udp_port)

    with qmivoice.QmiVoice(args.modem_dev).alloc_cid():
        await asyncio.gather(
            modem_manager.run(),
            matrix_client.sync_forever(full_state=True)
        )


if __name__ == '__main__':
    asyncio.run(main())
