# (c) 2020- Spiros Papadimitriou <spapadim@gmail.com>
#
# This file is released under the MIT License:
#    https://opensource.org/licenses/MIT
# This software is distributed on an "AS IS" basis,
# WITHOUT WARRANTY OF ANY KIND, either express or implied.


# Set defaults, if not already defined
INSTALL ?= /usr/bin/install
INSTALL_DATA ?= $(INSTALL) -m 0644
CLANG ?= /usr/bin/clang-9

ICON_PREFIX := $(DESTDIR)/usr/share/icons/hicolor


all: argonone.so

.PHONY: clean distclean install uninstall

argonone.so: argonone.c
	$(CC) $(CFLAGS) -Wall `pkg-config lxpanel --cflags` -shared -fPIC argonone.c -o argonone.so `pkg-config --libs lxpanel`

clean:
	rm -f argonone.so

distclean: clean
	rm -f compile_commands.json

install: SHELL:=/bin/bash
install: all
	# Install shared object
	$(INSTALL) -d $(DESTDIR)`pkg-config --variable=pluginsdir lxpanel`/
	# Install icons
	$(INSTALL) -m 0644 argonone.so $(DESTDIR)`pkg-config --variable=pluginsdir lxpanel`/
	for sz in 16 24; do \
		$(INSTALL) -d $(ICON_PREFIX)/$${sz}x$${sz}/status; \
		for icon in argonone-fan{,-paused,-medium,-high}; do \
			$(INSTALL_DATA) icons/$${icon}_$${sz}.png $(ICON_PREFIX)/$${sz}x$${sz}/status/$${icon}.png; \
		done; \
	done

uninstall: SHELL:=/bin/bash
uninstall:
	# Remove shared object
	rm -v $(DESTDIR)`pkg-config --variable=pluginsdir lxpanel`/argonone.so
	# Remove icons
	for sz in 16 24; do \
		for icon in argonone-fan{,-paused,-medium,-high}; do \
			rm -v $(ICON_PREFIX)/$${sz}x$${sz}/status/$${icon}.png; \
		done; \
	done

compile_commands.json: SHELL:=/bin/bash
compile_commands.json: argonone.c
	$(CLANG) -MJ >( sed -e '1s/^/[\n/' -e '$$s/,$$/\n]/' > compile_commands.json ) \
			$(CFLAGS) -Wall `pkg-config lxpanel --cflags` -shared -fPIC argonone.c -o argonone.so `pkg-config --libs lxpanel`
#	$(CLANG) -MJ argonone.so.json $(CFLAGS) -Wall `pkg-config lxpanel --cflags` -shared -fPIC argonone.c -o argonone.so `pkg-config --libs lxpanel`
#	sed -e '1s/^/[\n/' -e '$$s/,$/\n]/' *.so.json > compile_commands.json
#	rm *.so.json
