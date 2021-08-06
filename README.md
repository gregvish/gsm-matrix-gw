# GSM to Matrix Call Gateway Bot
GSM call forwarding to Matrix. A bot that forwards calls (or SMS) from a GSM/4G LTE modem to a Matrix room.

Designed to work on the following setup:

* A 4G modem with your SIM card connected at some location with stable Internet access
  * Port forwarding from the public Internet IP is enabled on your router at that location. The UDP port that is used for the actual VoIP calls needs to be forwarded.
* You set up a bot account on your Matrix homeserver, and chat with it in a private room. This bot will be forwarding SMS and calls to you in that room.

This was *actually* made to work on the following setup (in practice):

* Specifically, a Quectel EG25-G based modem. For example, [EG25-G USB modem from Aliexpress](https://www.aliexpress.com/item/4000140639655.html?spm=a2g0s.9042311.0.0.25e94c4dCiFyRj) (Separate IPX antenna required)
  * This is the same modem module that is used on the PinePhone (https://www.pine64.org/pinephone/) and in the Librem 5 design (https://puri.sm/products/librem-5/)
  * This works with an open source **custom firmware** for the modem: https://github.com/Biktorgj/pinephone_modem_sdk/
    * With the AT+EN_USBAUD feature
    * On original firmware, setting up voice calls is also possible with the `AT+QCFG="USBCFG",0x2C7C,0x0125,1,1,1,1,1,1,1` and `AT+QPCMV=1,2` commands.


# Prerequisites

This uses docker, and also assumes a proper setup of the hardware on the host environment. Essentially, the following udev rules should be placed (for examle) into `/etc/udev/rules.d/89-lte-modem.rules`:
```
ATTRS{idVendor}=="2c7c", ATTRS{idProduct}=="0125", ENV{PULSE_IGNORE}="1"
DRIVERS=="usb", KERNEL=="*cdc-wdm*", GROUP="dialout"
```
The Vendor and Product IDs here match the Quectel modem. This is necessary for the USB sound-card exposed by the modem to the host to be available for this software, and for permissions to be matching (`dialout` group, precisely).
Running `cat /proc/asound/cards` should show something like the following with regards to the Quectel modem soundcard:
```
$ cat /proc/asound/cards
...
 2 [Module         ]: USB-Audio - LTE Module
                      Quectel, Incorporated LTE Module at usb-0000:00:14.0-4.1, high speed
```
The name of the soundcard is `Module`.
Apart from this, the AT command port of the modem should be found, on a path like `/dev/ttyUSB2`, and the QMI port under `/dev/cdc-wdm0`. Both need to be usable by the `dialout` group on your host.

# Building and running
```
./doit.sh --homeserver <YOUR-HOMESERVER> --user <BOT-USER>  --password <BOT-PASSWORD> --udp_port 49572 --modem_tty /dev/ttyUSB2 --modem_dev /dev/cdc-wdm1
```
The `udp_port` can be any UDP port that you forwarded from your router to the host machine (has to be the same port number internally and externally).
This builds the docker image, and runs it as daemon that also survives reboots. The ouput can be seen using `docker logs -f gsm-matrix-gw-container`
