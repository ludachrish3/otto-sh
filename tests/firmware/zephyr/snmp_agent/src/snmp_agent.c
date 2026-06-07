/*
 * otto test-bed SNMP v2c agent (minimal, read-only).
 * ============================================================================
 * STATUS: compiles + links on Zephyr 3.7 (build-verified). Targets the Zephyr
 * 3.0+ `zephyr/`-prefixed include layout and the no-arg SYS_INIT signature, so
 * it is built only on 3.7/4.x (enabled via the per-version overlay supplements;
 * see ../common/otto-overlay-v3_7.conf). It does NOT build on 2.7, which uses
 * the old <kernel.h>-style includes and the (const struct device *) SYS_INIT
 * signature — 2.7 registers the module but leaves the agent off. Enabling 2.7
 * is a follow-up (version-guard the includes + init signature).
 *
 * The on-wire encoding is NOT yet validated end-to-end: confirm with
 * `snmpget` / `snmpwalk` against a running instance (see ../../README.md). The
 * BER OID byte tables below were computed by hand — the most likely bug site.
 * ============================================================================
 *
 * Why this exists: a Zephyr device exposes a single telnet shell session.
 * otto already drives that session for command execution, so performance
 * monitoring needs a *separate* channel that does not steal the console. This
 * agent is that channel — a tiny UDP/161 responder serving a fixed, read-only
 * OID table that otto's SNMP manager (src/otto/monitor/snmp.py) polls.
 *
 * Scope is deliberately tiny: SNMP v2c (community, no auth/priv), GET and
 * GETNEXT for a handful of scalar OIDs. No SET, no traps, no tables, no MIB
 * compiler. We hand-roll just enough BER to parse a request PDU and emit a
 * response PDU. This is far smaller and more self-contained than vendoring
 * lwIP's SNMP codec, which is coupled to lwIP's pbuf streams and its own
 * network stack (this build uses Zephyr's native stack + BSD sockets).
 *
 * Served OIDs (must stay in lockstep with _OTTO_BASE in otto/monitor/snmp.py):
 *   1.3.6.1.2.1.1.3.0          sysUpTime     TimeTicks  (1/100 s)
 *   1.3.6.1.4.1.63245.1.1.0    overall CPU   Gauge32    (centi-percent)
 *   1.3.6.1.4.1.63245.1.2.0    heap used     Gauge32    (bytes)
 *   1.3.6.1.4.1.63245.1.3.0    heap free     Gauge32    (bytes)
 *   1.3.6.1.4.1.63245.1.4.0    thread count  Gauge32
 */

#include <zephyr/kernel.h>
#include <zephyr/init.h>
#include <zephyr/logging/log.h>
#include <zephyr/net/socket.h>
#include <string.h>

#if defined(CONFIG_SYS_HEAP_RUNTIME_STATS)
#include <zephyr/sys/sys_heap.h>
/* The kernel's libc/k_malloc system heap. The in-tree `kernel heap` shell
 * command reads the same internal symbol; referencing it here is the
 * documented build-verify point — if a given LTS does not export it, drop
 * SYS_HEAP_RUNTIME_STATS for that version (the OIDs then report 0). */
extern struct sys_heap _system_heap;
#endif

LOG_MODULE_REGISTER(otto_snmp, LOG_LEVEL_INF);

/* ---- BER tags we use ---------------------------------------------------- */
#define BER_INTEGER   0x02
#define BER_OCTET_STR 0x04
#define BER_NULL      0x05
#define BER_OID       0x06
#define BER_SEQUENCE  0x30
#define SNMP_GAUGE32  0x42 /* APPLICATION 2 — unsigned, "current value"     */
#define SNMP_TIMETICKS 0x43 /* APPLICATION 3 — hundredths of a second        */
#define SNMP_PDU_GET     0xA0
#define SNMP_PDU_GETNEXT 0xA1
#define SNMP_PDU_RESPONSE 0xA2

#define SNMP_ERR_NO_ERROR     0
#define SNMP_ERR_NO_SUCH_NAME 2

/* ---- OID table ---------------------------------------------------------- */
/* Each entry stores the *content* bytes of the OID (the value of the OID TLV,
 * not the 0x06/length prefix). Sorted ascending so GETNEXT is a linear scan
 * for the first entry greater than the requested OID. */

enum otto_oid {
	OID_SYS_UPTIME = 0,
	OID_CPU,
	OID_HEAP_USED,
	OID_HEAP_FREE,
	OID_THREADS,
	OID_COUNT,
};

/* 1.3.6.1.2.1.1.3.0  -> 2B 06 01 02 01 01 03 00
 * Enterprise base 1.3.6.1.4.1.63245 -> 2B 06 01 04 01 [83 EE 0D]
 *   63245 = 0xF70D; base-128: 63245=3*128^2 + 110*128 + 13
 *   -> 0x83 0xEE 0x0D (high bit set on all but the last group). */
static const uint8_t oid_uptime[]    = {0x2B,0x06,0x01,0x02,0x01,0x01,0x03,0x00};
static const uint8_t oid_cpu[]       = {0x2B,0x06,0x01,0x04,0x01,0x83,0xEE,0x0D,0x01,0x01,0x00};
static const uint8_t oid_heap_used[] = {0x2B,0x06,0x01,0x04,0x01,0x83,0xEE,0x0D,0x01,0x02,0x00};
static const uint8_t oid_heap_free[] = {0x2B,0x06,0x01,0x04,0x01,0x83,0xEE,0x0D,0x01,0x03,0x00};
static const uint8_t oid_threads[]   = {0x2B,0x06,0x01,0x04,0x01,0x83,0xEE,0x0D,0x01,0x04,0x00};

struct oid_entry {
	const uint8_t *oid;
	uint8_t        len;
};

static const struct oid_entry oid_table[OID_COUNT] = {
	[OID_SYS_UPTIME] = {oid_uptime,    sizeof(oid_uptime)},
	[OID_CPU]        = {oid_cpu,       sizeof(oid_cpu)},
	[OID_HEAP_USED]  = {oid_heap_used, sizeof(oid_heap_used)},
	[OID_HEAP_FREE]  = {oid_heap_free, sizeof(oid_heap_free)},
	[OID_THREADS]    = {oid_threads,   sizeof(oid_threads)},
};

/* ---- live value providers ----------------------------------------------- */

static uint32_t read_uptime_centisecs(void)
{
	return (uint32_t)(k_uptime_get() / 10); /* ms -> hundredths of a second */
}

static uint32_t read_cpu_centipercent(void)
{
	k_thread_runtime_stats_t stats;

	/* Zephyr's k_thread_runtime_stats_all_get fills:
	 *   total_cycles     = non-idle (busy) cycles
	 *   execution_cycles = total elapsed = busy + idle
	 * (see kernel/usage.c: execution_cycles = total_cycles + idle_cycles).
	 * So utilization is busy/total = total_cycles / execution_cycles, in
	 * centi-percent. Do NOT compute total_cycles - idle_cycles: on a mostly
	 * idle system idle > busy and the unsigned subtraction underflows. Use a
	 * 64-bit intermediate — cycle counts overflow 32 bits quickly. */
	if (k_thread_runtime_stats_all_get(&stats) != 0 || stats.execution_cycles == 0) {
		return 0;
	}
	return (uint32_t)(((uint64_t)stats.total_cycles * 10000U) / stats.execution_cycles);
}

static void read_heap(uint32_t *used, uint32_t *free_bytes)
{
	*used = 0;
	*free_bytes = 0;
#if defined(CONFIG_SYS_HEAP_RUNTIME_STATS)
	struct sys_memory_stats st;

	if (sys_heap_runtime_stats_get(&_system_heap, &st) == 0) {
		*used = (uint32_t)st.allocated_bytes;
		*free_bytes = (uint32_t)st.free_bytes;
	}
#endif
}

static void count_thread_cb(const struct k_thread *thread, void *user)
{
	ARG_UNUSED(thread);
	(*(uint32_t *)user)++;
}

static uint32_t read_thread_count(void)
{
	uint32_t n = 0;

	k_thread_foreach(count_thread_cb, &n);
	return n;
}

static uint32_t oid_value(enum otto_oid which, uint8_t *ber_type)
{
	uint32_t used, freeb;

	switch (which) {
	case OID_SYS_UPTIME: *ber_type = SNMP_TIMETICKS; return read_uptime_centisecs();
	case OID_CPU:        *ber_type = SNMP_GAUGE32;   return read_cpu_centipercent();
	case OID_HEAP_USED:  *ber_type = SNMP_GAUGE32;   read_heap(&used, &freeb); return used;
	case OID_HEAP_FREE:  *ber_type = SNMP_GAUGE32;   read_heap(&used, &freeb); return freeb;
	case OID_THREADS:    *ber_type = SNMP_GAUGE32;   return read_thread_count();
	default:             *ber_type = BER_NULL;       return 0;
	}
}

/* ---- minimal BER reader ------------------------------------------------- */
/* All lengths in our traffic are < 128, so we only handle the short-form
 * length encoding. Anything longer is rejected (parse failure -> dropped). */

struct ber {
	const uint8_t *buf;
	size_t         len;
	size_t         pos;
};

static bool ber_tlv(struct ber *b, uint8_t *tag, const uint8_t **val, size_t *vlen)
{
	if (b->pos + 2 > b->len) {
		return false;
	}
	*tag = b->buf[b->pos++];
	uint8_t l = b->buf[b->pos++];

	if (l & 0x80) {          /* long-form length — unsupported here */
		return false;
	}
	if (b->pos + l > b->len) {
		return false;
	}
	*val = &b->buf[b->pos];
	*vlen = l;
	b->pos += l;
	return true;
}

/* Match a requested OID (GET) or find the next-greater entry (GETNEXT).
 * Returns the table index, or -1 on no match. */
static int oid_lookup(const uint8_t *oid, size_t len, bool next)
{
	for (int i = 0; i < OID_COUNT; i++) {
		const struct oid_entry *e = &oid_table[i];
		int cmp = memcmp(oid, e->oid, MIN(len, e->len));

		if (!next) {
			if (cmp == 0 && len == e->len) {
				return i;
			}
		} else {
			/* first entry strictly greater than the requested OID */
			if (cmp < 0 || (cmp == 0 && len < e->len)) {
				return i;
			}
		}
	}
	return -1;
}

/* ---- minimal BER writer ------------------------------------------------- */
/* Builds the response back-to-front is overkill for our fixed shapes; instead
 * we append forward and patch SEQUENCE lengths after writing their contents.
 * Every length stays < 128 (single varbind responses, tiny values), so the
 * one-byte length slot reserved by seq_start() is always sufficient. */

struct wb {
	uint8_t *buf;
	size_t   cap;
	size_t   pos;
	bool     ok;
};

static void wb_byte(struct wb *w, uint8_t v)
{
	if (!w->ok || w->pos >= w->cap) {
		w->ok = false;
		return;
	}
	w->buf[w->pos++] = v;
}

static void wb_bytes(struct wb *w, const uint8_t *p, size_t n)
{
	for (size_t i = 0; i < n; i++) {
		wb_byte(w, p[i]);
	}
}

/* Emit tag + 1-byte length placeholder; return the index of the length byte. */
static size_t wb_open(struct wb *w, uint8_t tag)
{
	wb_byte(w, tag);
	size_t lenpos = w->pos;

	wb_byte(w, 0x00);
	return lenpos;
}

/* Patch the length byte written by wb_open() with the content length. */
static void wb_close(struct wb *w, size_t lenpos)
{
	if (!w->ok) {
		return;
	}
	size_t content = w->pos - lenpos - 1;

	if (content > 0x7F) {  /* would need long-form — out of our envelope */
		w->ok = false;
		return;
	}
	w->buf[lenpos] = (uint8_t)content;
}

/* Encode an unsigned 32-bit as a (non-negative) BER INTEGER/Gauge/TimeTicks
 * content: minimal bytes, with a leading 0x00 if the top bit would set. */
static void wb_uint(struct wb *w, uint8_t tag, uint32_t v)
{
	uint8_t tmp[5];
	int n = 0;

	/* big-endian minimal encoding */
	do {
		tmp[n++] = (uint8_t)(v & 0xFF);
		v >>= 8;
	} while (v != 0);
	if (tmp[n - 1] & 0x80) {
		tmp[n++] = 0x00; /* keep it unsigned */
	}
	wb_byte(w, tag);
	wb_byte(w, (uint8_t)n);
	for (int i = n - 1; i >= 0; i--) {
		wb_byte(w, tmp[i]);
	}
}

/* ---- request handling --------------------------------------------------- */
/* Parse one request datagram and build the response into *out. Returns the
 * response length, or 0 to drop the packet silently (malformed / not v2c). */
static size_t handle_request(const uint8_t *in, size_t in_len, uint8_t *out, size_t out_cap)
{
	struct ber b = {.buf = in, .len = in_len, .pos = 0};
	uint8_t tag;
	const uint8_t *v;
	size_t vlen;

	/* outer SEQUENCE */
	if (!ber_tlv(&b, &tag, &v, &vlen) || tag != BER_SEQUENCE) {
		return 0;
	}
	struct ber msg = {.buf = v, .len = vlen, .pos = 0};

	/* version INTEGER — accept v2c (1). (v1==0 also works for our scalars,
	 * but otto polls v2c; keep the check tight.) */
	if (!ber_tlv(&msg, &tag, &v, &vlen) || tag != BER_INTEGER || vlen != 1 || v[0] != 1) {
		return 0;
	}
	/* community OCTET STRING — accepted without checking the value (the
	 * relay/test bed is trusted; tighten if this ever faces a real net). */
	if (!ber_tlv(&msg, &tag, &v, &vlen) || tag != BER_OCTET_STR) {
		return 0;
	}
	/* PDU */
	if (!ber_tlv(&msg, &tag, &v, &vlen)) {
		return 0;
	}
	bool getnext;

	if (tag == SNMP_PDU_GET) {
		getnext = false;
	} else if (tag == SNMP_PDU_GETNEXT) {
		getnext = true;
	} else {
		return 0; /* SET / unsupported */
	}
	struct ber pdu = {.buf = v, .len = vlen, .pos = 0};

	/* request-id INTEGER (echoed verbatim) */
	const uint8_t *reqid;
	size_t reqid_len;

	if (!ber_tlv(&pdu, &tag, &reqid, &reqid_len) || tag != BER_INTEGER) {
		return 0;
	}
	/* error-status, error-index (ignored on input) */
	if (!ber_tlv(&pdu, &tag, &v, &vlen) || tag != BER_INTEGER) {
		return 0;
	}
	if (!ber_tlv(&pdu, &tag, &v, &vlen) || tag != BER_INTEGER) {
		return 0;
	}
	/* varbind list SEQUENCE */
	if (!ber_tlv(&pdu, &tag, &v, &vlen) || tag != BER_SEQUENCE) {
		return 0;
	}
	struct ber vbl = {.buf = v, .len = vlen, .pos = 0};

	/* Build the response. Layout mirrors the request:
	 *   SEQ { version, community, RESPONSE-PDU { reqid, err, idx, varbinds } } */
	struct wb w = {.buf = out, .cap = out_cap, .pos = 0, .ok = true};
	size_t msg_seq = wb_open(&w, BER_SEQUENCE);

	wb_byte(&w, BER_INTEGER); wb_byte(&w, 1); wb_byte(&w, 1);   /* version v2c */
	wb_byte(&w, BER_OCTET_STR); wb_byte(&w, 6);                 /* community  */
	wb_bytes(&w, (const uint8_t *)"public", 6);

	size_t pdu_seq = wb_open(&w, SNMP_PDU_RESPONSE);

	wb_byte(&w, BER_INTEGER); wb_byte(&w, (uint8_t)reqid_len);  /* echo reqid */
	wb_bytes(&w, reqid, reqid_len);

	/* error-status / error-index are patched only if a varbind misses. */
	size_t errstat_pos = w.pos + 2;

	wb_byte(&w, BER_INTEGER); wb_byte(&w, 1); wb_byte(&w, SNMP_ERR_NO_ERROR);
	size_t erridx_pos = w.pos + 2;

	wb_byte(&w, BER_INTEGER); wb_byte(&w, 1); wb_byte(&w, 0);

	size_t vbl_seq = wb_open(&w, BER_SEQUENCE);

	int vb_index = 0;
	while (vbl.pos < vbl.len) {
		/* each varbind: SEQUENCE { OID, value } — request value is NULL */
		if (!ber_tlv(&vbl, &tag, &v, &vlen) || tag != BER_SEQUENCE) {
			return 0;
		}
		struct ber vb = {.buf = v, .len = vlen, .pos = 0};
		const uint8_t *oid;
		size_t oid_len;

		if (!ber_tlv(&vb, &tag, &oid, &oid_len) || tag != BER_OID) {
			return 0;
		}
		vb_index++;

		int idx = oid_lookup(oid, oid_len, getnext);

		size_t vb_seq = wb_open(&w, BER_SEQUENCE);

		if (idx < 0) {
			/* echo the requested OID + noSuchName error pointing at it */
			wb_byte(&w, BER_OID); wb_byte(&w, (uint8_t)oid_len);
			wb_bytes(&w, oid, oid_len);
			wb_byte(&w, BER_NULL); wb_byte(&w, 0);
			if (w.ok) {
				out[errstat_pos] = SNMP_ERR_NO_SUCH_NAME;
				out[erridx_pos]  = (uint8_t)vb_index;
			}
		} else {
			const struct oid_entry *e = &oid_table[idx];
			uint8_t vtype;
			uint32_t val = oid_value((enum otto_oid)idx, &vtype);

			wb_byte(&w, BER_OID); wb_byte(&w, e->len);
			wb_bytes(&w, e->oid, e->len);
			wb_uint(&w, vtype, val);
		}
		wb_close(&w, vb_seq);
	}

	wb_close(&w, vbl_seq);
	wb_close(&w, pdu_seq);
	wb_close(&w, msg_seq);

	return w.ok ? w.pos : 0;
}

/* ---- listener thread ---------------------------------------------------- */

#define RX_BUF_SIZE 512

static void snmp_agent_thread(void *a, void *b, void *c)
{
	ARG_UNUSED(a); ARG_UNUSED(b); ARG_UNUSED(c);

	int sock = zsock_socket(AF_INET, SOCK_DGRAM, IPPROTO_UDP);

	if (sock < 0) {
		LOG_ERR("socket() failed: %d", errno);
		return;
	}

	struct sockaddr_in addr = {
		.sin_family = AF_INET,
		.sin_addr.s_addr = INADDR_ANY,
		.sin_port = htons(CONFIG_OTTO_SNMP_AGENT_PORT),
	};

	if (zsock_bind(sock, (struct sockaddr *)&addr, sizeof(addr)) < 0) {
		LOG_ERR("bind(%d) failed: %d", CONFIG_OTTO_SNMP_AGENT_PORT, errno);
		zsock_close(sock);
		return;
	}
	LOG_INF("otto SNMP agent listening on UDP/%d", CONFIG_OTTO_SNMP_AGENT_PORT);

	static uint8_t rx[RX_BUF_SIZE];
	static uint8_t tx[RX_BUF_SIZE];

	for (;;) {
		struct sockaddr_in peer;
		socklen_t peer_len = sizeof(peer);

		ssize_t n = zsock_recvfrom(sock, rx, sizeof(rx), 0,
					   (struct sockaddr *)&peer, &peer_len);
		if (n <= 0) {
			continue;
		}
		size_t resp = handle_request(rx, (size_t)n, tx, sizeof(tx));

		if (resp > 0) {
			zsock_sendto(sock, tx, resp, 0,
				     (struct sockaddr *)&peer, peer_len);
		}
	}
}

K_THREAD_STACK_DEFINE(snmp_agent_stack, CONFIG_OTTO_SNMP_AGENT_STACK_SIZE);
static struct k_thread snmp_agent_tid;

static int otto_snmp_agent_init(void)
{
	k_thread_create(&snmp_agent_tid, snmp_agent_stack,
			K_THREAD_STACK_SIZEOF(snmp_agent_stack),
			snmp_agent_thread, NULL, NULL, NULL,
			CONFIG_OTTO_SNMP_AGENT_THREAD_PRIO, 0, K_NO_WAIT);
	k_thread_name_set(&snmp_agent_tid, "otto_snmp");
	return 0;
}

/* Start after networking is up. APPLICATION/99 lands late in init, by which
 * point CONFIG_NET_CONFIG_SETTINGS has assigned the static IPv4 address. */
SYS_INIT(otto_snmp_agent_init, APPLICATION, 99);
