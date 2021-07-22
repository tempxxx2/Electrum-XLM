SHELL := /bin/bash
PYTHON = python3

# for reproducible builds
export LC_ALL             := C
export TZ                 := UTC
export SOURCE_DATE_EPOCH  := $(shell git log -1 --pretty=%ct)
export PYTHONHASHSEED     := $(SOURCE_DATE_EPOCH)
export BUILD_DATE         := $(shell LC_ALL=C TZ=UTC date +'%b %e %Y' -d @$(SOURCE_DATE_EPOCH))
export BUILD_TIME         := $(shell LC_ALL=C TZ=UTC date +'%H:%M:%S' -d @$(SOURCE_DATE_EPOCH))

# needs kivy installed or in PYTHONPATH

.PHONY: theming prepare prepare-testnet apk apk-testnet release release-testnet clean

theming:
	$(PYTHON) -m kivy.atlas ../../electrum_dash/gui/kivy/theming/atlas/light 1024 ../../electrum_dash/gui/kivy/theming/light/*.png
prepare:
	# running pre build setup
	@cp buildozer.spec ../../buildozer.spec
	# copy electrum-dash to main.py
	@cp ../../electrum-dash ../../main.py
prepare-testnet:
	@-cd ../..; sed -r "s:^(title =.*):\1 Testnet:" -i buildozer.spec
	@-cd ../..; sed -r "s:^(package.nam.*):\1_Testnet:" -i buildozer.spec
	@-cd ../..; sed -r "s:^(presp.*).png:\1_testnet.png:" -i buildozer.spec
	@-cd ../..; sed -r "s:^(icon.filename.*).png:\1_testnet.png:" -i buildozer.spec
	@-cd ../..; sed -r "s:^(icon.adaptive_foreground.*).png:\1_testnet.png:" -i buildozer.spec
apk:
	@make prepare
	@-cd ../..; buildozer android debug
	@make clean
apk-testnet:
	@make prepare
	@make prepare-testnet
	@-cd ../..; buildozer android debug
	@make clean
release:
	@make prepare
	@-cd ../..; buildozer android release
	@make clean
release-testnet:
	@make prepare
	@make prepare-testnet
	@-cd ../..; buildozer android release
	@make clean
clean:
	# Cleaning up
	# rename main.py to electrum
	@-rm ../../main.py
	# remove buildozer.spec
	@-rm ../../buildozer.spec
