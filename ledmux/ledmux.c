/*
 * ledmux.c - PiDP-11 front panel LED/switch multiplexer for statuspanel.
 *
 * Drives the 6x12 LED matrix and scans the 3x12 switch matrix of the
 * PiDP-11 board. LED state comes from a shared-memory file written by
 * statuspanel.py; raw switch state is written back to the same file.
 *
 * Pin assignments and timing follow Oscar Vermeulen's gpio.c:
 *   LED rows (UDN2981 high-side drivers): GPIO 20..25  (high = row on)
 *   Columns (shared LEDs/switches):       GPIO 26,27,4..13 (low = LED lit)
 *   Switch rows:                          GPIO 16,17,18 (pull low to read)
 *
 * If the writer stops updating the heartbeat for more than STALE_SECONDS,
 * the whole panel flashes slowly so a dead monitor is never mistaken for
 * "everything healthy".
 *
 * Build: gcc -O2 -Wall -o ledmux ledmux.c
 * Usage: ledmux [row_on_us]   (default 400)
 */
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <time.h>
#include <unistd.h>

#define SHM_PATH "/dev/shm/statuspanel"
#define SHM_MAGIC 0x31504450u /* "PDP1" little-endian */
#define STALE_SECONDS 6

/* Must match SHM layout in statuspanel.py ("<III6H3HxxI8I", 68 bytes) */
struct panel_shm {
	uint32_t magic;
	uint32_t version;
	volatile uint32_t heartbeat;    /* bumped by statuspanel.py */
	volatile uint16_t leds[6];      /* bit k = column k lit */
	volatile uint16_t switches[3];  /* raw scan; bit k = column k read high */
	uint16_t pad;
	volatile uint32_t scan_count;   /* bumped by ledmux every frame */
	uint32_t reserved[8];
};

static const uint8_t ledrows[6] = {20, 21, 22, 23, 24, 25};
static const uint8_t swrows[3] = {16, 17, 18};
static const uint8_t cols[12] = {26, 27, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13};

static volatile uint32_t *gpio;
static volatile sig_atomic_t stop;

#define GPFSEL(n) gpio[(n)]
#define GPSET0 gpio[7]
#define GPCLR0 gpio[10]
#define GPLEV0 gpio[13]
#define GPPUD gpio[37]
#define GPPUDCLK0 gpio[38]
#define GPPUPPDN(n) gpio[57 + (n)] /* BCM2711 (Pi 4) pull registers */

static void pin_input(int g) { GPFSEL(g / 10) &= ~(7u << ((g % 10) * 3)); }
static void pin_output(int g)
{
	pin_input(g);
	GPFSEL(g / 10) |= (1u << ((g % 10) * 3));
}

static void sleep_ns(long ns)
{
	struct timespec ts = {ns / 1000000000L, ns % 1000000000L};
	while (clock_nanosleep(CLOCK_MONOTONIC, 0, &ts, &ts) == EINTR && !stop)
		;
}

/* pull: 0 = none, 1 = up, 2 = down (BCM2711 encoding) */
static void set_pull(const uint8_t *pins, int n, int pull)
{
	int is2711 = (gpio[57] != 0x6770696f); /* "gpio" = unimplemented reg */
	if (is2711) {
		for (int i = 0; i < n; i++) {
			int g = pins[i], reg = g / 16, shift = (g % 16) * 2;
			uint32_t v = GPPUPPDN(reg);
			v &= ~(3u << shift);
			v |= ((uint32_t)pull << shift);
			GPPUPPDN(reg) = v;
		}
	} else {
		uint32_t mask = 0;
		for (int i = 0; i < n; i++)
			mask |= 1u << pins[i];
		GPPUD = (pull == 1) ? 2 : (pull == 2) ? 1 : 0;
		sleep_ns(10000);
		GPPUDCLK0 = mask;
		sleep_ns(10000);
		GPPUD = 0;
		GPPUDCLK0 = 0;
	}
}

static void all_inputs(void)
{
	for (int i = 0; i < 6; i++) {
		GPCLR0 = 1u << ledrows[i];
		pin_input(ledrows[i]);
	}
	for (int i = 0; i < 12; i++)
		pin_input(cols[i]);
	for (int i = 0; i < 3; i++)
		pin_input(swrows[i]);
}

static void on_signal(int sig) { (void)sig; stop = 1; }

static struct panel_shm *open_shm(void)
{
	int fd = open(SHM_PATH, O_RDWR | O_CREAT, 0666);
	if (fd < 0) {
		perror("open " SHM_PATH);
		return NULL;
	}
	fchmod(fd, 0666);
	struct stat st;
	if (fstat(fd, &st) == 0 && st.st_size < (off_t)sizeof(struct panel_shm)) {
		if (ftruncate(fd, sizeof(struct panel_shm)) != 0) {
			perror("ftruncate");
			close(fd);
			return NULL;
		}
	}
	struct panel_shm *s = mmap(NULL, sizeof(*s), PROT_READ | PROT_WRITE, MAP_SHARED, fd, 0);
	close(fd);
	if (s == MAP_FAILED) {
		perror("mmap shm");
		return NULL;
	}
	if (s->magic != SHM_MAGIC) {
		memset(s, 0, sizeof(*s));
		s->version = 1;
		s->magic = SHM_MAGIC;
	}
	return s;
}

static double now_s(void)
{
	struct timespec ts;
	clock_gettime(CLOCK_MONOTONIC, &ts);
	return ts.tv_sec + ts.tv_nsec / 1e9;
}

int main(int argc, char **argv)
{
	long row_on_ns = (argc > 1 ? atol(argv[1]) : 400) * 1000L;
	if (row_on_ns < 20000)
		row_on_ns = 20000;

	int fd = open("/dev/gpiomem", O_RDWR | O_SYNC);
	if (fd < 0) {
		perror("open /dev/gpiomem");
		return 1;
	}
	gpio = mmap(NULL, 4096, PROT_READ | PROT_WRITE, MAP_SHARED, fd, 0);
	close(fd);
	if (gpio == MAP_FAILED) {
		perror("mmap gpio");
		return 1;
	}

	struct panel_shm *shm = open_shm();
	if (!shm)
		return 1;

	signal(SIGINT, on_signal);
	signal(SIGTERM, on_signal);

	all_inputs();
	set_pull(cols, 12, 1);    /* columns pulled up for switch reads */
	set_pull(ledrows, 6, 0);
	set_pull(swrows, 3, 0);

	uint32_t last_hb = shm->heartbeat;
	double last_hb_change = now_s();
	uint16_t frame[6];

	fprintf(stderr, "ledmux: running, row time %ld us\n", row_on_ns / 1000);

	while (!stop) {
		double t = now_s();
		uint32_t hb = shm->heartbeat;
		if (hb != last_hb) {
			last_hb = hb;
			last_hb_change = t;
		}
		int stale = (t - last_hb_change) > STALE_SECONDS;
		for (int r = 0; r < 6; r++)
			frame[r] = stale ? (((long)(t * 2) & 1) ? 0x0fff : 0) : shm->leds[r];

		/* LEDs */
		for (int i = 0; i < 12; i++)
			pin_output(cols[i]);
		for (int r = 0; r < 6; r++) {
			uint32_t set = 0, clr = 0;
			for (int k = 0; k < 12; k++) {
				if (frame[r] & (1u << k))
					clr |= 1u << cols[k];
				else
					set |= 1u << cols[k];
			}
			GPSET0 = set;
			GPCLR0 = clr;
			GPSET0 = 1u << ledrows[r];
			pin_output(ledrows[r]);
			sleep_ns(row_on_ns);
			GPCLR0 = 1u << ledrows[r];
			sleep_ns(10000); /* let UDN2981 turn off: avoids ghosting */
		}

		/* Switches */
		for (int i = 0; i < 12; i++)
			pin_input(cols[i]);
		for (int r = 0; r < 3; r++) {
			pin_output(swrows[r]);
			GPCLR0 = 1u << swrows[r];
			sleep_ns(5000);
			uint32_t lev = GPLEV0;
			uint16_t v = 0;
			for (int k = 0; k < 12; k++)
				if (lev & (1u << cols[k]))
					v |= 1u << k;
			pin_input(swrows[r]);
			shm->switches[r] = v;
		}
		shm->scan_count++;
	}

	all_inputs();
	fprintf(stderr, "ledmux: stopped\n");
	return 0;
}
