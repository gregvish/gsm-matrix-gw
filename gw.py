import os
import asyncio
import logging
import argparse
import functools

import qmivoice
from matrixapi import (
    do_matrix_login, MatrixCallForwarder, MatrixSmsForwarder, MatrixEventHandler,
    udp_random_port_monkeypatch
)
from quectelmodem import QuectelModemManager


logger = logging.getLogger('GsmGw')


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
    parser.add_argument('--sim_pin', help='SIM card PIN', default=None)
    parser.add_argument('--preferred_network', help='GSM/UMTS/LTE', default='LTE')
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
        args.modem_tty,
        call_forwarder=matrix_call_fwd,
        sms_forwarder=matrix_sms_fwd,
        sim_card_pin=args.sim_pin,
        preferred_network=args.preferred_network
    )

    udp_random_port_monkeypatch(args.udp_port)

    with qmivoice.QmiVoice(args.modem_dev).alloc_cid():
        await asyncio.gather(
            modem_manager.run(),
            matrix_client.sync_forever(loop_sleep_time=500, full_state=True)
        )


if __name__ == '__main__':
    asyncio.run(main())
