/*
 * VibeOS Network Stack — self-contained for AMC verification.
 *
 * Source: https://github.com/kaansenol5/VibeOS/blob/main/kernel/net.c
 * Changes: stubbed virtio_net/printf/string headers; inlined net.h types.
 *
 * Key properties CBMC checks:
 *  - ip_checksum: loop termination; no OOB read on `data`
 *  - icmp_handle: data_len doesn't exceed reply_buf bounds (stack overflow)
 *  - ip_send: ip_buf[1600] write within bounds; memcpy len check
 *  - udp_handle: udp_len parsing stays within `pkt` bounds
 *  - arp_add: arp_table circular write never OOB
 */

#include <stdint.h>
#include <stddef.h>

/* --- stubs for external functions --- */
static void *memcpy(void *d, const void *s, size_t n) {
    char *dd = (char *)d; const char *ss = (const char *)s;
    for (size_t i = 0; i < n; i++) dd[i] = ss[i];
    return d;
}
static void *memset(void *s, int c, size_t n) {
    char *p = (char *)s;
    for (size_t i = 0; i < n; i++) p[i] = (char)c;
    return s;
}
static void printf(const char *fmt, ...) { (void)fmt; }

/* --- NET_MTU (from virtio_net.h) --- */
#define NET_MTU 1514

/* stubs for virtio_net functions */
static void virtio_net_get_mac(uint8_t *mac) { (void)mac; }
static int  virtio_net_send(const uint8_t *buf, int len) { (void)buf; (void)len; return 0; }
static int  virtio_net_has_packet(void) { return 0; }
static int  virtio_net_recv(uint8_t *buf, int max) { (void)buf; (void)max; return 0; }

/* --- net.h types inlined --- */
typedef struct __attribute__((packed)) {
    uint8_t dst[6];
    uint8_t src[6];
    uint16_t ethertype;
} eth_header_t;

#define ETH_TYPE_IP   0x0800
#define ETH_TYPE_ARP  0x0806

typedef struct __attribute__((packed)) {
    uint16_t htype;
    uint16_t ptype;
    uint8_t hlen;
    uint8_t plen;
    uint16_t oper;
    uint8_t sha[6];
    uint8_t spa[4];
    uint8_t tha[6];
    uint8_t tpa[4];
} arp_packet_t;

#define ARP_OP_REQUEST  1
#define ARP_OP_REPLY    2

typedef struct __attribute__((packed)) {
    uint8_t version_ihl;
    uint8_t tos;
    uint16_t total_len;
    uint16_t id;
    uint16_t flags_frag;
    uint8_t ttl;
    uint8_t protocol;
    uint16_t checksum;
    uint32_t src_ip;
    uint32_t dst_ip;
} ip_header_t;

#define IP_PROTO_ICMP  1
#define IP_PROTO_TCP   6
#define IP_PROTO_UDP   17

typedef struct __attribute__((packed)) {
    uint8_t type;
    uint8_t code;
    uint16_t checksum;
    uint16_t id;
    uint16_t seq;
} icmp_header_t;

#define ICMP_ECHO_REPLY    0
#define ICMP_ECHO_REQUEST  8

typedef struct __attribute__((packed)) {
    uint16_t src_port;
    uint16_t dst_port;
    uint16_t length;
    uint16_t checksum;
} udp_header_t;

typedef struct {
    uint32_t ip;
    uint8_t mac[6];
    uint8_t valid;
    uint32_t timestamp;
} arp_entry_t;

#define NET_IP          0x0a00020f
#define NET_GATEWAY     0x0a000202
#define NET_DNS         0x0a000203
#define NET_NETMASK     0xffffff00
#define MAKE_IP(a,b,c,d) (((uint32_t)(a)<<24)|((uint32_t)(b)<<16)|((uint32_t)(c)<<8)|(uint32_t)(d))

typedef void (*udp_recv_callback_t)(uint32_t src_ip, uint16_t src_port, uint16_t dst_port, const void *data, uint32_t len);

typedef struct __attribute__((packed)) {
    uint16_t src_port;
    uint16_t dst_port;
    uint32_t seq;
    uint32_t ack;
    uint8_t data_off;
    uint8_t flags;
    uint16_t window;
    uint16_t checksum;
    uint16_t urgent;
} tcp_header_t;

#define TCP_FIN  0x01
#define TCP_SYN  0x02
#define TCP_RST  0x04
#define TCP_PSH  0x08
#define TCP_ACK  0x10
#define TCP_URG  0x20
#define TCP_STATE_CLOSED      0
#define TCP_STATE_SYN_SENT    1
#define TCP_STATE_ESTABLISHED 2
#define TCP_STATE_FIN_WAIT_1  3
#define TCP_STATE_FIN_WAIT_2  4
#define TCP_STATE_CLOSE_WAIT  5
#define TCP_STATE_LAST_ACK    6
#define TCP_STATE_TIME_WAIT   7
typedef int tcp_socket_t;

/* --- original net.c (unmodified logic) --- */

/*
 * VibeOS Network Stack
 *
 * Ethernet, ARP, IP, ICMP implementation
 */

/* #include "net.h" -- replaced by inline stubs above */
/* #include "virtio_net.h" -- replaced by inline stubs above */
/* #include "printf.h" -- replaced by inline stubs above */
/* #include "string.h" -- replaced by inline stubs above */

// Our MAC and IP
static uint8_t our_mac[6];
static uint32_t our_ip = NET_IP;

// ARP table
#define ARP_TABLE_SIZE 16
static arp_entry_t arp_table[ARP_TABLE_SIZE];

// Packet buffer
static uint8_t pkt_buf[1600];

// Broadcast MAC
static const uint8_t broadcast_mac[6] = {0xff, 0xff, 0xff, 0xff, 0xff, 0xff};

// Ping state (for tracking echo replies)
static volatile int ping_received = 0;
static volatile uint16_t ping_id = 0;
static volatile uint16_t ping_seq = 0;

// UDP listener table
#define UDP_MAX_LISTENERS 8
typedef struct {
    uint16_t port;
    udp_recv_callback_t callback;
} udp_listener_t;
static udp_listener_t udp_listeners[UDP_MAX_LISTENERS];

// Byte order helpers (network = big endian)
static inline uint16_t htons(uint16_t x) {
    return (x >> 8) | (x << 8);
}

static inline uint16_t ntohs(uint16_t x) {
    return htons(x);
}

static inline uint32_t htonl(uint32_t x) {
    return ((x >> 24) & 0xff) |
           ((x >> 8) & 0xff00) |
           ((x << 8) & 0xff0000) |
           ((x << 24) & 0xff000000);
}

static inline uint32_t ntohl(uint32_t x) {
    return htonl(x);
}

// IP to string (static buffer - not thread safe, but we're single-threaded)
static char ip_str_buf[16];
const char *ip_to_str(uint32_t ip) {
    uint8_t *b = (uint8_t *)&ip;
    // IP is stored in network order (big endian), so first byte is most significant
    int len = 0;
    for (int i = 0; i < 4; i++) {
        int val = b[3-i];  // Reverse for our MAKE_IP macro (host order)
        if (val >= 100) {
            ip_str_buf[len++] = '0' + val / 100;
            ip_str_buf[len++] = '0' + (val / 10) % 10;
            ip_str_buf[len++] = '0' + val % 10;
        } else if (val >= 10) {
            ip_str_buf[len++] = '0' + val / 10;
            ip_str_buf[len++] = '0' + val % 10;
        } else {
            ip_str_buf[len++] = '0' + val;
        }
        if (i < 3) ip_str_buf[len++] = '.';
    }
    ip_str_buf[len] = '\0';
    return ip_str_buf;
}

void net_init(void) {
    // Get our MAC from the driver
    virtio_net_get_mac(our_mac);

    // Clear ARP table
    memset(arp_table, 0, sizeof(arp_table));

    printf("[NET] Stack initialized, IP=%s\n", ip_to_str(our_ip));
}

uint32_t net_get_ip(void) {
    return our_ip;
}

void net_get_mac(uint8_t *mac) {
    memcpy(mac, our_mac, 6);
}

// Send ethernet frame
int eth_send(const uint8_t *dst_mac, uint16_t ethertype, const void *data, uint32_t len) {
    if (len > NET_MTU - sizeof(eth_header_t)) {
        return -1;
    }

    // Build frame
    eth_header_t *eth = (eth_header_t *)pkt_buf;
    memcpy(eth->dst, dst_mac, 6);
    memcpy(eth->src, our_mac, 6);
    eth->ethertype = htons(ethertype);

    memcpy(pkt_buf + sizeof(eth_header_t), data, len);

    return virtio_net_send(pkt_buf, sizeof(eth_header_t) + len);
}

// ARP table lookup
const uint8_t *arp_lookup(uint32_t ip) {
    for (int i = 0; i < ARP_TABLE_SIZE; i++) {
        if (arp_table[i].valid && arp_table[i].ip == ip) {
            return arp_table[i].mac;
        }
    }
    return NULL;
}

// Add/update ARP entry
static void arp_add(uint32_t ip, const uint8_t *mac) {
    // Check if already exists
    for (int i = 0; i < ARP_TABLE_SIZE; i++) {
        if (arp_table[i].valid && arp_table[i].ip == ip) {
            memcpy(arp_table[i].mac, mac, 6);
            return;
        }
    }

    // Find free slot
    for (int i = 0; i < ARP_TABLE_SIZE; i++) {
        if (!arp_table[i].valid) {
            arp_table[i].ip = ip;
            memcpy(arp_table[i].mac, mac, 6);
            arp_table[i].valid = 1;
            printf("[ARP] Added %s -> %02x:%02x:%02x:%02x:%02x:%02x\n",
                   ip_to_str(ip), mac[0], mac[1], mac[2], mac[3], mac[4], mac[5]);
            return;
        }
    }

    // Table full - overwrite first entry (simple LRU)
    arp_table[0].ip = ip;
    memcpy(arp_table[0].mac, mac, 6);
    arp_table[0].valid = 1;
}

// Send ARP request
void arp_request(uint32_t ip) {
    arp_packet_t arp;
    arp.htype = htons(1);        // Ethernet
    arp.ptype = htons(0x0800);   // IPv4
    arp.hlen = 6;
    arp.plen = 4;
    arp.oper = htons(ARP_OP_REQUEST);
    memcpy(arp.sha, our_mac, 6);

    // Convert our IP to network byte order
    uint32_t our_ip_net = htonl(our_ip);
    memcpy(arp.spa, &our_ip_net, 4);

    memset(arp.tha, 0, 6);       // Unknown
    uint32_t ip_net = htonl(ip);
    memcpy(arp.tpa, &ip_net, 4);

    printf("[ARP] Requesting %s\n", ip_to_str(ip));
    eth_send(broadcast_mac, ETH_TYPE_ARP, &arp, sizeof(arp));
}

// Handle incoming ARP packet
static void arp_handle(const uint8_t *pkt, uint32_t len) {
    if (len < sizeof(arp_packet_t)) return;

    const arp_packet_t *arp = (const arp_packet_t *)pkt;

    // Only handle Ethernet/IPv4
    if (ntohs(arp->htype) != 1 || ntohs(arp->ptype) != 0x0800) return;
    if (arp->hlen != 6 || arp->plen != 4) return;

    uint32_t sender_ip, target_ip;
    memcpy(&sender_ip, arp->spa, 4);
    memcpy(&target_ip, arp->tpa, 4);
    sender_ip = ntohl(sender_ip);
    target_ip = ntohl(target_ip);

    uint16_t op = ntohs(arp->oper);

    // Learn sender's MAC (even if not for us)
    arp_add(sender_ip, arp->sha);

    if (op == ARP_OP_REQUEST) {
        // Is this asking for our MAC?
        if (target_ip == our_ip) {
            printf("[ARP] Request for our IP from %s\n", ip_to_str(sender_ip));

            // Send reply
            arp_packet_t reply;
            reply.htype = htons(1);
            reply.ptype = htons(0x0800);
            reply.hlen = 6;
            reply.plen = 4;
            reply.oper = htons(ARP_OP_REPLY);
            memcpy(reply.sha, our_mac, 6);
            uint32_t our_ip_net = htonl(our_ip);
            memcpy(reply.spa, &our_ip_net, 4);
            memcpy(reply.tha, arp->sha, 6);
            memcpy(reply.tpa, arp->spa, 4);

            eth_send(arp->sha, ETH_TYPE_ARP, &reply, sizeof(reply));
            printf("[ARP] Sent reply\n");
        }
    } else if (op == ARP_OP_REPLY) {
        printf("[ARP] Reply from %s\n", ip_to_str(sender_ip));
        // Already added to table above
    }
}

// IP checksum
uint16_t ip_checksum(const void *data, uint32_t len) {
    const uint16_t *ptr = (const uint16_t *)data;
    uint32_t sum = 0;

    while (len > 1) {
        sum += *ptr++;
        len -= 2;
    }

    if (len == 1) {
        sum += *(const uint8_t *)ptr;
    }

    while (sum >> 16) {
        sum = (sum & 0xffff) + (sum >> 16);
    }

    return ~sum;
}

// Handle incoming ICMP packet
static void icmp_handle(const uint8_t *pkt, uint32_t len, uint32_t src_ip) {
    if (len < sizeof(icmp_header_t)) return;

    const icmp_header_t *icmp = (const icmp_header_t *)pkt;

    if (icmp->type == ICMP_ECHO_REQUEST) {
        printf("[ICMP] Echo request from %s\n", ip_to_str(src_ip));

        // Send echo reply
        uint8_t reply_buf[1500];
        icmp_header_t *reply = (icmp_header_t *)reply_buf;

        reply->type = ICMP_ECHO_REPLY;
        reply->code = 0;
        reply->checksum = 0;
        reply->id = icmp->id;
        reply->seq = icmp->seq;

        // Copy data portion
        uint32_t data_len = len - sizeof(icmp_header_t);
        if (data_len > sizeof(reply_buf) - sizeof(icmp_header_t)) {
            data_len = sizeof(reply_buf) - sizeof(icmp_header_t);
        }
        memcpy(reply_buf + sizeof(icmp_header_t), pkt + sizeof(icmp_header_t), data_len);

        // Calculate checksum
        reply->checksum = ip_checksum(reply_buf, sizeof(icmp_header_t) + data_len);

        ip_send(src_ip, IP_PROTO_ICMP, reply_buf, sizeof(icmp_header_t) + data_len);
        printf("[ICMP] Sent echo reply\n");
    }
    else if (icmp->type == ICMP_ECHO_REPLY) {
        printf("[ICMP] Echo reply from %s id=%d seq=%d\n",
               ip_to_str(src_ip), ntohs(icmp->id), ntohs(icmp->seq));

        // Check if this matches our pending ping
        if (ntohs(icmp->id) == ping_id && ntohs(icmp->seq) == ping_seq) {
            ping_received = 1;
        }
    }
}

// Handle incoming UDP packet
static void udp_handle(const uint8_t *pkt, uint32_t len, uint32_t src_ip) {
    if (len < sizeof(udp_header_t)) return;

    const udp_header_t *udp = (const udp_header_t *)pkt;
    uint16_t src_port = ntohs(udp->src_port);
    uint16_t dst_port = ntohs(udp->dst_port);
    uint16_t udp_len = ntohs(udp->length);

    if (udp_len < sizeof(udp_header_t) || udp_len > len) return;

    const uint8_t *data = pkt + sizeof(udp_header_t);
    uint32_t data_len = udp_len - sizeof(udp_header_t);

    // Find listener for this port
    for (int i = 0; i < UDP_MAX_LISTENERS; i++) {
        if (udp_listeners[i].callback && udp_listeners[i].port == dst_port) {
            udp_listeners[i].callback(src_ip, src_port, dst_port, data, data_len);
            return;
        }
    }

    // No listener - silently drop (don't spam debug output)
}

// Forward declaration for TCP handler
static void tcp_handle(const uint8_t *pkt, uint32_t len, uint32_t src_ip);

// Handle incoming IP packet
static void ip_handle(const uint8_t *pkt, uint32_t len) {
    if (len < sizeof(ip_header_t)) return;

    const ip_header_t *ip = (const ip_header_t *)pkt;

    // Check version
    if ((ip->version_ihl >> 4) != 4) return;

    // Get header length
    uint32_t ihl = (ip->version_ihl & 0x0f) * 4;
    if (ihl < 20 || ihl > len) return;

    // Check if it's for us
    uint32_t dst_ip = ntohl(ip->dst_ip);
    if (dst_ip != our_ip && dst_ip != 0xffffffff) return;

    uint32_t src_ip = ntohl(ip->src_ip);
    uint32_t payload_len = ntohs(ip->total_len) - ihl;
    const uint8_t *payload = pkt + ihl;

    switch (ip->protocol) {
        case IP_PROTO_ICMP:
            icmp_handle(payload, payload_len, src_ip);
            break;
        case IP_PROTO_UDP:
            udp_handle(payload, payload_len, src_ip);
            break;
        case IP_PROTO_TCP:
            tcp_handle(payload, payload_len, src_ip);
            break;
        default:
            printf("[IP] Unknown protocol %d from %s\n", ip->protocol, ip_to_str(src_ip));
            break;
    }
}

// Send IP packet
int ip_send(uint32_t dst_ip, uint8_t protocol, const void *data, uint32_t len) {
    if (len > NET_MTU - sizeof(eth_header_t) - sizeof(ip_header_t)) {
        return -1;
    }

    // Determine next hop MAC
    const uint8_t *dst_mac;
    uint32_t next_hop = dst_ip;

    // If destination is not on local network, use gateway
    if ((dst_ip & NET_NETMASK) != (our_ip & NET_NETMASK)) {
        next_hop = NET_GATEWAY;
    }

    dst_mac = arp_lookup(next_hop);
    if (!dst_mac) {
        // Need to ARP first
        printf("[IP] No ARP entry for %s, sending request\n", ip_to_str(next_hop));
        arp_request(next_hop);
        return -1;  // Caller should retry
    }

    // Build IP packet
    static uint8_t ip_buf[1600];
    ip_header_t *ip = (ip_header_t *)ip_buf;

    ip->version_ihl = 0x45;  // IPv4, 20 byte header
    ip->tos = 0;
    ip->total_len = htons(sizeof(ip_header_t) + len);
    ip->id = htons(0);  // TODO: track ID
    ip->flags_frag = 0;
    ip->ttl = 64;
    ip->protocol = protocol;
    ip->checksum = 0;
    ip->src_ip = htonl(our_ip);
    ip->dst_ip = htonl(dst_ip);

    // Calculate header checksum
    ip->checksum = ip_checksum(ip, sizeof(ip_header_t));

    // Copy payload
    memcpy(ip_buf + sizeof(ip_header_t), data, len);

    return eth_send(dst_mac, ETH_TYPE_IP, ip_buf, sizeof(ip_header_t) + len);
}

// Send ICMP echo request
int icmp_send_echo_request(uint32_t dst_ip, uint16_t id, uint16_t seq, const void *data, uint32_t len) {
    uint8_t icmp_buf[1500];
    icmp_header_t *icmp = (icmp_header_t *)icmp_buf;

    icmp->type = ICMP_ECHO_REQUEST;
    icmp->code = 0;
    icmp->checksum = 0;
    icmp->id = htons(id);
    icmp->seq = htons(seq);

    // Copy data
    if (len > sizeof(icmp_buf) - sizeof(icmp_header_t)) {
        len = sizeof(icmp_buf) - sizeof(icmp_header_t);
    }
    if (data && len > 0) {
        memcpy(icmp_buf + sizeof(icmp_header_t), data, len);
    }

    // Calculate checksum
    icmp->checksum = ip_checksum(icmp_buf, sizeof(icmp_header_t) + len);

    return ip_send(dst_ip, IP_PROTO_ICMP, icmp_buf, sizeof(icmp_header_t) + len);
}

// Process incoming packets
void net_poll(void) {
    static uint8_t rx_buf[1600];

    while (virtio_net_has_packet()) {
        int len = virtio_net_recv(rx_buf, sizeof(rx_buf));
        if (len <= 0) break;

        if (len < (int)sizeof(eth_header_t)) continue;

        eth_header_t *eth = (eth_header_t *)rx_buf;
        uint16_t ethertype = ntohs(eth->ethertype);

        const uint8_t *payload = rx_buf + sizeof(eth_header_t);
        uint32_t payload_len = len - sizeof(eth_header_t);

        switch (ethertype) {
            case ETH_TYPE_ARP:
                arp_handle(payload, payload_len);
                break;
            case ETH_TYPE_IP:
                ip_handle(payload, payload_len);
                break;
            default:
                // Ignore unknown ethertypes
                break;
        }
    }
}

// Blocking ping with timeout
int net_ping(uint32_t ip, uint16_t seq, uint32_t timeout_ms) {
    // First, make sure we have ARP entry for the target (or gateway)
    uint32_t next_hop = ip;
    if ((ip & NET_NETMASK) != (our_ip & NET_NETMASK)) {
        next_hop = NET_GATEWAY;
    }

    // Try to get ARP entry, send request if needed
    if (!arp_lookup(next_hop)) {
        arp_request(next_hop);

        // Wait for ARP reply (up to 1 second)
        for (int i = 0; i < 100 && !arp_lookup(next_hop); i++) {
            net_poll();
            // Simple delay (~10ms)
            for (volatile int j = 0; j < 100000; j++);
        }

        if (!arp_lookup(next_hop)) {
            printf("[PING] ARP timeout for %s\n", ip_to_str(next_hop));
            return -1;
        }
    }

    // Set up ping tracking
    ping_id = 0x1234;
    ping_seq = seq;
    ping_received = 0;

    // Send echo request
    uint8_t ping_data[56];
    memset(ping_data, 0xAB, sizeof(ping_data));

    if (icmp_send_echo_request(ip, ping_id, seq, ping_data, sizeof(ping_data)) < 0) {
        return -1;
    }

    // Wait for reply
    for (uint32_t i = 0; i < timeout_ms / 10 && !ping_received; i++) {
        net_poll();
        // Simple delay (~10ms)
        for (volatile int j = 0; j < 100000; j++);
    }

    if (ping_received) {
        return 0;  // TODO: return actual RTT
    }

    return -1;  // Timeout
}

// UDP bind - register a listener for a port
void udp_bind(uint16_t port, udp_recv_callback_t callback) {
    // Check if already bound
    for (int i = 0; i < UDP_MAX_LISTENERS; i++) {
        if (udp_listeners[i].port == port && udp_listeners[i].callback) {
            // Replace existing listener
            udp_listeners[i].callback = callback;
            return;
        }
    }

    // Find free slot
    for (int i = 0; i < UDP_MAX_LISTENERS; i++) {
        if (!udp_listeners[i].callback) {
            udp_listeners[i].port = port;
            udp_listeners[i].callback = callback;
            return;
        }
    }

    printf("[UDP] No free listener slots!\n");
}

// UDP unbind - remove a listener
void udp_unbind(uint16_t port) {
    for (int i = 0; i < UDP_MAX_LISTENERS; i++) {
        if (udp_listeners[i].port == port) {
            udp_listeners[i].callback = NULL;
            udp_listeners[i].port = 0;
            return;
        }
    }
}

// Send UDP packet
int udp_send(uint32_t dst_ip, uint16_t src_port, uint16_t dst_port, const void *data, uint32_t len) {
    if (len > NET_MTU - sizeof(eth_header_t) - sizeof(ip_header_t) - sizeof(udp_header_t)) {
        return -1;
    }

    // Build UDP packet
    uint8_t udp_buf[1500];
    udp_header_t *udp = (udp_header_t *)udp_buf;

    udp->src_port = htons(src_port);
    udp->dst_port = htons(dst_port);
    udp->length = htons(sizeof(udp_header_t) + len);
    udp->checksum = 0;  // Checksum optional for IPv4

    // Copy data
    memcpy(udp_buf + sizeof(udp_header_t), data, len);

    return ip_send(dst_ip, IP_PROTO_UDP, udp_buf, sizeof(udp_header_t) + len);
}

// DNS resolver
// Simple implementation - sends query to NET_DNS and waits for response

// DNS header
typedef struct __attribute__((packed)) {
    uint16_t id;
    uint16_t flags;
    uint16_t qdcount;
    uint16_t ancount;
    uint16_t nscount;
    uint16_t arcount;
} dns_header_t;

// DNS response state
static volatile int dns_response_received = 0;
static volatile uint32_t dns_resolved_ip = 0;
static volatile uint16_t dns_query_id = 0;

// DNS response handler
static void dns_recv_handler(uint32_t src_ip, uint16_t src_port, uint16_t dst_port, const void *data, uint32_t len) {
    (void)src_ip; (void)src_port; (void)dst_port;

    if (len < sizeof(dns_header_t)) return;

    const dns_header_t *dns = (const dns_header_t *)data;

    // Check if this is response to our query
    if (ntohs(dns->id) != dns_query_id) return;

    // Check flags: QR=1 (response), RCODE=0 (no error)
    uint16_t flags = ntohs(dns->flags);
    if (!(flags & 0x8000)) return;  // Not a response
    if (flags & 0x000f) return;     // Error code set

    uint16_t ancount = ntohs(dns->ancount);
    if (ancount == 0) return;  // No answers

    // Skip header and question section
    const uint8_t *ptr = (const uint8_t *)data + sizeof(dns_header_t);
    const uint8_t *end = (const uint8_t *)data + len;

    // Skip question (QNAME + QTYPE + QCLASS)
    // QNAME is a sequence of labels ending with 0
    while (ptr < end && *ptr != 0) {
        if ((*ptr & 0xc0) == 0xc0) {
            // Pointer - skip 2 bytes
            ptr += 2;
            goto parse_answer;
        }
        ptr += 1 + *ptr;  // Skip length byte + label
    }
    if (ptr < end) ptr++;  // Skip null terminator
    ptr += 4;  // Skip QTYPE (2) + QCLASS (2)

parse_answer:
    // Parse answer records
    for (int i = 0; i < ancount && ptr + 12 <= end; i++) {
        // Skip NAME (may be pointer)
        if ((*ptr & 0xc0) == 0xc0) {
            ptr += 2;  // Pointer
        } else {
            while (ptr < end && *ptr != 0) {
                ptr += 1 + *ptr;
            }
            if (ptr < end) ptr++;
        }

        if (ptr + 10 > end) break;

        uint16_t type = (ptr[0] << 8) | ptr[1];
        // uint16_t class = (ptr[2] << 8) | ptr[3];
        // uint32_t ttl = (ptr[4] << 24) | (ptr[5] << 16) | (ptr[6] << 8) | ptr[7];
        uint16_t rdlength = (ptr[8] << 8) | ptr[9];
        ptr += 10;

        if (ptr + rdlength > end) break;

        // Type A = 1 (IPv4 address)
        if (type == 1 && rdlength == 4) {
            // Found an A record!
            dns_resolved_ip = MAKE_IP(ptr[0], ptr[1], ptr[2], ptr[3]);
            dns_response_received = 1;
            return;
        }

        ptr += rdlength;
    }
}

// Resolve hostname to IP
// Check if string is an IP address (e.g., "10.0.2.2") and parse it
static uint32_t parse_ip_string(const char *str) {
    uint8_t octets[4];
    int octet_idx = 0;
    int current = 0;
    int digits = 0;

    for (const char *p = str; ; p++) {
        if (*p >= '0' && *p <= '9') {
            current = current * 10 + (*p - '0');
            digits++;
            if (current > 255 || digits > 3) return 0;  // Invalid
        } else if (*p == '.' || *p == '\0') {
            if (digits == 0) return 0;  // No digits before dot
            if (octet_idx >= 4) return 0;  // Too many octets
            octets[octet_idx++] = current;
            current = 0;
            digits = 0;
            if (*p == '\0') break;
        } else {
            return 0;  // Invalid character - not an IP
        }
    }

    if (octet_idx != 4) return 0;  // Need exactly 4 octets
    return MAKE_IP(octets[0], octets[1], octets[2], octets[3]);
}

uint32_t dns_resolve(const char *hostname) {
    // First check if it's already an IP address
    uint32_t ip = parse_ip_string(hostname);
    if (ip != 0) {
        return ip;  // It's an IP, no DNS needed
    }

    // Build DNS query
    uint8_t query[512];
    dns_header_t *dns = (dns_header_t *)query;

    // Use a simple incrementing ID
    static uint16_t next_id = 1;
    dns_query_id = next_id++;

    dns->id = htons(dns_query_id);
    dns->flags = htons(0x0100);  // RD (recursion desired)
    dns->qdcount = htons(1);
    dns->ancount = 0;
    dns->nscount = 0;
    dns->arcount = 0;

    // Build QNAME from hostname
    uint8_t *ptr = query + sizeof(dns_header_t);
    const char *src = hostname;

    while (*src) {
        // Find next dot or end
        const char *dot = src;
        while (*dot && *dot != '.') dot++;

        uint8_t label_len = dot - src;
        if (label_len > 63) return 0;  // Label too long

        *ptr++ = label_len;
        while (src < dot) {
            *ptr++ = *src++;
        }
        if (*src == '.') src++;
    }
    *ptr++ = 0;  // Null terminator

    // QTYPE = A (1), QCLASS = IN (1)
    *ptr++ = 0; *ptr++ = 1;  // QTYPE
    *ptr++ = 0; *ptr++ = 1;  // QCLASS

    uint32_t query_len = ptr - query;

    // Bind to receive DNS responses on port 53 (we're the client, but use same port for simplicity)
    // Actually, use ephemeral port
    uint16_t local_port = 10053 + (dns_query_id % 100);
    dns_response_received = 0;
    dns_resolved_ip = 0;

    udp_bind(local_port, dns_recv_handler);

    // First, make sure we can reach DNS server (ARP)
    uint32_t dns_server = NET_DNS;
    uint32_t next_hop = dns_server;
    if ((dns_server & NET_NETMASK) != (our_ip & NET_NETMASK)) {
        next_hop = NET_GATEWAY;
    }

    if (!arp_lookup(next_hop)) {
        arp_request(next_hop);
        for (int i = 0; i < 100 && !arp_lookup(next_hop); i++) {
            net_poll();
            for (volatile int j = 0; j < 100000; j++);
        }
        if (!arp_lookup(next_hop)) {
            udp_unbind(local_port);
            return 0;
        }
    }

    // Send DNS query
    if (udp_send(dns_server, local_port, 53, query, query_len) < 0) {
        udp_unbind(local_port);
        return 0;
    }

    // Wait for response (up to 5 seconds)
    for (int i = 0; i < 500 && !dns_response_received; i++) {
        net_poll();
        for (volatile int j = 0; j < 100000; j++);
    }

    udp_unbind(local_port);

    if (dns_response_received) {
        printf("[DNS] Resolved %s -> %s\n", hostname, ip_to_str(dns_resolved_ip));
        return dns_resolved_ip;
    }

    printf("[DNS] Failed to resolve %s\n", hostname);
    return 0;
}

// ============ TCP Implementation ============

// TCP socket structure
#define TCP_MAX_SOCKETS 8
#define TCP_RX_BUF_SIZE 32768  // 32KB - TLS certs can be large
#define TCP_TX_BUF_SIZE 4096

typedef struct {
    int state;
    uint32_t local_ip;
    uint32_t remote_ip;
    uint16_t local_port;
    uint16_t remote_port;

    // Sequence numbers
    uint32_t send_seq;      // Next byte we'll send
    uint32_t send_ack;      // Last ACK we sent (next byte we expect)
    uint32_t recv_seq;      // For tracking incoming data

    // Receive buffer (ring buffer)
    uint8_t rx_buf[TCP_RX_BUF_SIZE];
    uint32_t rx_head;       // Write position
    uint32_t rx_tail;       // Read position

    // Flags
    uint8_t fin_received;   // Remote sent FIN
    uint8_t fin_sent;       // We sent FIN
} tcp_socket_internal_t;

static tcp_socket_internal_t tcp_sockets[TCP_MAX_SOCKETS];
static uint16_t tcp_next_port = 49152;  // Ephemeral port range

// TCP pseudo-header for checksum
typedef struct __attribute__((packed)) {
    uint32_t src_ip;
    uint32_t dst_ip;
    uint8_t zero;
    uint8_t protocol;
    uint16_t tcp_len;
} tcp_pseudo_header_t;

// Calculate TCP checksum (includes pseudo-header)
static uint16_t tcp_checksum(uint32_t src_ip, uint32_t dst_ip,
                              const tcp_header_t *tcp, const void *data, uint32_t data_len) {
    uint32_t sum = 0;

    // Pseudo-header
    sum += (src_ip >> 16) & 0xffff;
    sum += src_ip & 0xffff;
    sum += (dst_ip >> 16) & 0xffff;
    sum += dst_ip & 0xffff;
    sum += htons(IP_PROTO_TCP);
    sum += htons(sizeof(tcp_header_t) + data_len);

    // TCP header
    const uint16_t *ptr = (const uint16_t *)tcp;
    for (int i = 0; i < (int)(sizeof(tcp_header_t) / 2); i++) {
        sum += ptr[i];
    }

    // Data
    ptr = (const uint16_t *)data;
    while (data_len > 1) {
        sum += *ptr++;
        data_len -= 2;
    }
    if (data_len == 1) {
        sum += *(const uint8_t *)ptr;
    }

    // Fold 32-bit sum to 16 bits
    while (sum >> 16) {
        sum = (sum & 0xffff) + (sum >> 16);
    }

    return ~sum;
}

// Send a TCP segment
static int tcp_send_segment(tcp_socket_internal_t *sock, uint8_t flags,
                            const void *data, uint32_t len) {
    uint8_t pkt[1500];
    tcp_header_t *tcp = (tcp_header_t *)pkt;

    tcp->src_port = htons(sock->local_port);
    tcp->dst_port = htons(sock->remote_port);
    tcp->seq = htonl(sock->send_seq);
    tcp->ack = htonl(sock->send_ack);
    tcp->data_off = (5 << 4);  // 20 bytes, no options
    tcp->flags = flags;
    tcp->window = htons(TCP_RX_BUF_SIZE);
    tcp->checksum = 0;
    tcp->urgent = 0;

    // Copy data
    if (data && len > 0) {
        memcpy(pkt + sizeof(tcp_header_t), data, len);
    }

    // Calculate checksum
    tcp->checksum = tcp_checksum(htonl(sock->local_ip), htonl(sock->remote_ip),
                                  tcp, data, len);

    return ip_send(sock->remote_ip, IP_PROTO_TCP, pkt, sizeof(tcp_header_t) + len);
}

// Find socket by connection tuple
static tcp_socket_internal_t *tcp_find_socket(uint32_t remote_ip, uint16_t remote_port,
                                               uint16_t local_port) {
    for (int i = 0; i < TCP_MAX_SOCKETS; i++) {
        tcp_socket_internal_t *s = &tcp_sockets[i];
        if (s->state != TCP_STATE_CLOSED &&
            s->remote_ip == remote_ip &&
            s->remote_port == remote_port &&
            s->local_port == local_port) {
            return s;
        }
    }
    return NULL;
}

// Get socket index
static int tcp_socket_index(tcp_socket_internal_t *sock) {
    return sock - tcp_sockets;
}

// Handle incoming TCP packet
static void tcp_handle(const uint8_t *pkt, uint32_t len, uint32_t src_ip) {
    if (len < sizeof(tcp_header_t)) return;

    const tcp_header_t *tcp = (const tcp_header_t *)pkt;
    uint16_t src_port = ntohs(tcp->src_port);
    uint16_t dst_port = ntohs(tcp->dst_port);
    uint32_t seq = ntohl(tcp->seq);
    uint32_t ack = ntohl(tcp->ack);
    uint8_t flags = tcp->flags;

    // Calculate data offset and length
    uint32_t data_off = (tcp->data_off >> 4) * 4;
    if (data_off < sizeof(tcp_header_t) || data_off > len) return;

    const uint8_t *data = pkt + data_off;
    uint32_t data_len = len - data_off;

    // Find matching socket
    tcp_socket_internal_t *sock = tcp_find_socket(src_ip, src_port, dst_port);
    if (!sock) {
        // No socket - send RST if not a RST
        if (!(flags & TCP_RST)) {
            // TODO: send RST
        }
        return;
    }

    // Handle RST
    if (flags & TCP_RST) {
        printf("[TCP] Connection reset by peer\n");
        sock->state = TCP_STATE_CLOSED;
        return;
    }

    // State machine
    switch (sock->state) {
        case TCP_STATE_SYN_SENT:
            // Expecting SYN+ACK
            if ((flags & (TCP_SYN | TCP_ACK)) == (TCP_SYN | TCP_ACK)) {
                if (ack == sock->send_seq + 1) {
                    sock->send_seq = ack;
                    sock->send_ack = seq + 1;
                    sock->recv_seq = seq + 1;

                    // Send ACK
                    tcp_send_segment(sock, TCP_ACK, NULL, 0);
                    sock->state = TCP_STATE_ESTABLISHED;
                    printf("[TCP] Connection established\n");
                }
            }
            break;

        case TCP_STATE_ESTABLISHED:
            // Check if ACK is valid
            if (flags & TCP_ACK) {
                // Update send_seq if this ACKs new data
                if (ack > sock->send_seq - 1000 && ack <= sock->send_seq + 10000) {
                    // Valid ACK range (rough check)
                }
            }

            // Handle incoming data
            if (data_len > 0) {
                // Check if this is the next expected segment
                if (seq == sock->send_ack) {
                    // Copy data to receive buffer, track how many bytes actually stored
                    uint32_t bytes_stored = 0;
                    for (uint32_t i = 0; i < data_len; i++) {
                        uint32_t next_head = (sock->rx_head + 1) % TCP_RX_BUF_SIZE;
                        if (next_head == sock->rx_tail) {
                            // Buffer full - stop here
                            break;
                        }
                        sock->rx_buf[sock->rx_head] = data[i];
                        sock->rx_head = next_head;
                        bytes_stored++;
                    }

                    // CRITICAL: Only ACK bytes we actually stored!
                    // Otherwise we tell sender we got data that was dropped.
                    sock->send_ack = seq + bytes_stored;

                    // Send ACK
                    tcp_send_segment(sock, TCP_ACK, NULL, 0);
                }
            }

            // Handle FIN
            if (flags & TCP_FIN) {
                sock->fin_received = 1;
                sock->send_ack = seq + data_len + 1;
                tcp_send_segment(sock, TCP_ACK, NULL, 0);
                sock->state = TCP_STATE_CLOSE_WAIT;
                printf("[TCP] Received FIN, connection closing\n");
            }
            break;

        case TCP_STATE_FIN_WAIT_1:
            if (flags & TCP_ACK) {
                if (flags & TCP_FIN) {
                    // Simultaneous close
                    sock->send_ack = seq + 1;
                    tcp_send_segment(sock, TCP_ACK, NULL, 0);
                    sock->state = TCP_STATE_TIME_WAIT;
                } else {
                    sock->state = TCP_STATE_FIN_WAIT_2;
                }
            }
            break;

        case TCP_STATE_FIN_WAIT_2:
            if (flags & TCP_FIN) {
                sock->send_ack = seq + 1;
                tcp_send_segment(sock, TCP_ACK, NULL, 0);
                sock->state = TCP_STATE_TIME_WAIT;
            }
            break;

        case TCP_STATE_CLOSE_WAIT:
            // Waiting for application to close
            break;

        case TCP_STATE_LAST_ACK:
            if (flags & TCP_ACK) {
                sock->state = TCP_STATE_CLOSED;
                printf("[TCP] Connection closed\n");
            }
            break;

        case TCP_STATE_TIME_WAIT:
            // Should wait 2*MSL, but we just close immediately
            sock->state = TCP_STATE_CLOSED;
            break;

        default:
            break;
    }
}

// Public API

tcp_socket_t tcp_connect(uint32_t ip, uint16_t port) {
    // Find free socket
    int idx = -1;
    for (int i = 0; i < TCP_MAX_SOCKETS; i++) {
        if (tcp_sockets[i].state == TCP_STATE_CLOSED) {
            idx = i;
            break;
        }
    }
    if (idx < 0) {
        printf("[TCP] No free sockets\n");
        return -1;
    }

    tcp_socket_internal_t *sock = &tcp_sockets[idx];
    memset(sock, 0, sizeof(*sock));

    sock->local_ip = our_ip;
    sock->remote_ip = ip;
    sock->local_port = tcp_next_port++;
    sock->remote_port = port;
    sock->send_seq = 1000 + (tcp_next_port * 1234);  // Simple ISN
    sock->send_ack = 0;
    sock->state = TCP_STATE_SYN_SENT;

    // ARP resolve first
    uint32_t next_hop = ip;
    if ((ip & NET_NETMASK) != (our_ip & NET_NETMASK)) {
        next_hop = NET_GATEWAY;
    }

    if (!arp_lookup(next_hop)) {
        arp_request(next_hop);
        for (int i = 0; i < 100 && !arp_lookup(next_hop); i++) {
            net_poll();
            for (volatile int j = 0; j < 100000; j++);
        }
        if (!arp_lookup(next_hop)) {
            printf("[TCP] ARP failed for %s\n", ip_to_str(next_hop));
            sock->state = TCP_STATE_CLOSED;
            return -1;
        }
    }

    // Send SYN
    printf("[TCP] Connecting to %s:%d\n", ip_to_str(ip), port);
    if (tcp_send_segment(sock, TCP_SYN, NULL, 0) < 0) {
        sock->state = TCP_STATE_CLOSED;
        return -1;
    }

    // Wait for SYN+ACK (up to 10 seconds)
    for (int i = 0; i < 1000 && sock->state == TCP_STATE_SYN_SENT; i++) {
        net_poll();
        for (volatile int j = 0; j < 100000; j++);
    }

    if (sock->state != TCP_STATE_ESTABLISHED) {
        printf("[TCP] Connection timeout\n");
        sock->state = TCP_STATE_CLOSED;
        return -1;
    }

    return idx;
}

int tcp_send(tcp_socket_t sock_id, const void *data, uint32_t len) {
    if (sock_id < 0 || sock_id >= TCP_MAX_SOCKETS) return -1;

    tcp_socket_internal_t *sock = &tcp_sockets[sock_id];
    if (sock->state != TCP_STATE_ESTABLISHED) return -1;

    // Send data in chunks (MSS ~1460, use 1400 to be safe)
    const uint8_t *ptr = (const uint8_t *)data;
    uint32_t sent = 0;

    while (sent < len) {
        uint32_t chunk = len - sent;
        if (chunk > 1400) chunk = 1400;

        if (tcp_send_segment(sock, TCP_ACK | TCP_PSH, ptr + sent, chunk) < 0) {
            return sent > 0 ? (int)sent : -1;
        }

        sock->send_seq += chunk;
        sent += chunk;

        // Small delay between segments
        for (volatile int j = 0; j < 10000; j++);
    }

    return (int)sent;
}

int tcp_recv(tcp_socket_t sock_id, void *buf, uint32_t maxlen) {
    if (sock_id < 0 || sock_id >= TCP_MAX_SOCKETS) return -1;

    tcp_socket_internal_t *sock = &tcp_sockets[sock_id];

    // Poll for incoming data
    net_poll();

    // Check for data in receive buffer
    uint8_t *dst = (uint8_t *)buf;
    uint32_t received = 0;

    while (received < maxlen && sock->rx_tail != sock->rx_head) {
        dst[received++] = sock->rx_buf[sock->rx_tail];
        sock->rx_tail = (sock->rx_tail + 1) % TCP_RX_BUF_SIZE;
    }

    // If no data and connection closed, return -1
    if (received == 0) {
        if (sock->state == TCP_STATE_CLOSE_WAIT ||
            sock->state == TCP_STATE_CLOSED) {
            return -1;
        }
        return 0;  // No data yet
    }

    return (int)received;
}

void tcp_close(tcp_socket_t sock_id) {
    if (sock_id < 0 || sock_id >= TCP_MAX_SOCKETS) return;

    tcp_socket_internal_t *sock = &tcp_sockets[sock_id];

    if (sock->state == TCP_STATE_ESTABLISHED) {
        // Send FIN
        tcp_send_segment(sock, TCP_FIN | TCP_ACK, NULL, 0);
        sock->send_seq++;
        sock->fin_sent = 1;
        sock->state = TCP_STATE_FIN_WAIT_1;

        // Wait for close to complete (up to 5 seconds)
        for (int i = 0; i < 500 && sock->state != TCP_STATE_CLOSED &&
                                   sock->state != TCP_STATE_TIME_WAIT; i++) {
            net_poll();
            for (volatile int j = 0; j < 100000; j++);
        }
    } else if (sock->state == TCP_STATE_CLOSE_WAIT) {
        // Send FIN
        tcp_send_segment(sock, TCP_FIN | TCP_ACK, NULL, 0);
        sock->send_seq++;
        sock->state = TCP_STATE_LAST_ACK;

        // Wait for ACK
        for (int i = 0; i < 500 && sock->state != TCP_STATE_CLOSED; i++) {
            net_poll();
            for (volatile int j = 0; j < 100000; j++);
        }
    }

    sock->state = TCP_STATE_CLOSED;
}

int tcp_is_connected(tcp_socket_t sock_id) {
    if (sock_id < 0 || sock_id >= TCP_MAX_SOCKETS) return 0;
    return tcp_sockets[sock_id].state == TCP_STATE_ESTABLISHED;
}

int tcp_get_state(tcp_socket_t sock_id) {
    if (sock_id < 0 || sock_id >= TCP_MAX_SOCKETS) return TCP_STATE_CLOSED;
    return tcp_sockets[sock_id].state;
}
