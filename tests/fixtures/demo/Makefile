# Minimal build used by the Weaver tests.  CC and CFLAGS select the profile.
CC ?= cc
CFLAGS ?= -O2 -std=c11
BUILD ?= build
SRCS := src/main.c src/alias.c src/edge.c src/util.c
OBJS := $(SRCS:src/%.c=$(BUILD)/%.o)

$(BUILD)/demo: $(OBJS)
	$(CC) $(CFLAGS) -o $@ $(OBJS)

$(BUILD)/%.o: src/%.c | $(BUILD)
	$(CC) $(CFLAGS) -Iinclude -c $< -o $@

$(BUILD):
	mkdir -p $(BUILD)

clean:
	rm -rf $(BUILD)

.PHONY: clean
