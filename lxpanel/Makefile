# (c) 2020- Spiros Papadimitriou <spapadim@gmail.com>
#
# This file is released under the MIT License:
#    https://opensource.org/licenses/MIT
# This software is distributed on an "AS IS" basis,
# WITHOUT WARRANTY OF ANY KIND, either express or implied.

# Set defaults, if not already defined
INSTALL ?= /usr/bin/install
INSTALL_DATA ?= $(INSTALL) -m 0644


all: argonone.so

argonone.so: argonone.c
	$(CC) $(CFLAGS) -Wall `pkg-config lxpanel --cflags` -shared -fPIC argonone.c -o argonone.so `pkg-config --libs lxpanel`

clean:
	rm -f argonone.so

install: all
	$(INSTALL) -m 0644 argonone.so $(DESTDIR)`pkg-config --variable=pluginsdir lxpanel`/
	$(INSTALL_DATA) argonone-fan_16.png $(DESTDIR)/usr/share/icons/hicolor/16x16/status/argonone-fan.png
	$(INSTALL_DATA) argonone-fan_24.png $(DESTDIR)/usr/share/icons/hicolor/24x24/status/argonone-fan.png
	$(INSTALL_DATA) argonone-fan-paused_16.png $(DESTDIR)/usr/share/icons/hicolor/16x16/status/argonone-fan-paused.png
	$(INSTALL_DATA) argonone-fan-paused_24.png $(DESTDIR)/usr/share/icons/hicolor/24x24/status/argonone-fan-paused.png

uninstall:
	rm -v $(DESTDIR)`pkg-config --variable=pluginsdir lxpanel`/argonone.so
	rm -v $(DESTDIR)/usr/share/icons/hicolor/{16x16,24x24}/status/argonone-fan.png
	rm -v $(DESTDIR)/usr/share/icons/hicolor/{16x16,24x24}/status/argonone-fan-paused.png

