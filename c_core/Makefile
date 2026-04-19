# nishro_core Makefile (MinGW-w64 on Windows).
# Requires Npcap SDK at C:/Npcap-SDK (override with NPCAP_SDK=...).

NPCAP_SDK ?= C:/Npcap-SDK

CC      := gcc
CFLAGS  := -std=c11 -O2 -Wall -Wextra -Wno-unused-parameter \
           -D_WIN32_WINNT=0x0601 -DWIN32_LEAN_AND_MEAN \
           -I. -I"$(NPCAP_SDK)/Include"
LDFLAGS := -L"$(NPCAP_SDK)/Lib/x64" -static-libgcc
LDLIBS  := -lwpcap -lws2_32 -liphlpapi

SRCS := util.c packet.c pcap_io.c tftp.c config.c json.c acl.c \
        ftp.c source.c session.c arp.c icmp.c ipc.c main.c
OBJS := $(SRCS:.c=.o)
BIN  := bin/nishro_core.exe

all: $(BIN)

$(BIN): $(OBJS) | bin
	$(CC) $(OBJS) -o $@ $(LDFLAGS) $(LDLIBS)
	@echo "built $@"

bin:
	@mkdir -p bin

%.o: %.c
	$(CC) $(CFLAGS) -c $< -o $@

clean:
	rm -f $(OBJS) $(BIN)

.PHONY: all clean
