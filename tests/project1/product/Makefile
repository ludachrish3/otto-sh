CC ?= gcc
CFLAGS = --coverage -O0 -g
SRCS = main.c math_ops.c
TARGET = product

.PHONY: all clean

all: $(TARGET)

$(TARGET): $(SRCS)
	$(CC) $(CFLAGS) -o $@ $^

clean:
	rm -f $(TARGET) *.gcno *.gcda
