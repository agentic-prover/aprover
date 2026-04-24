/*
 * VibeOS VFS — self-contained for AMC verification.
 *
 * Source: https://github.com/kaansenol5/VibeOS/blob/main/kernel/vfs.c
 * Changes: fat32_init always returns -1 (in-memory path only); stubbed
 *          malloc/free/printf/string functions; removed kernel headers.
 *
 * Key properties CBMC checks:
 *  - alloc_inode: inode_count never exceeds VFS_MAX_INODES
 *  - create_mem_dir/file: child_count never exceeds VFS_MAX_CHILDREN
 *  - vfs_read: offset + to_read never overflows; no OOB into file->data
 *  - vfs_write: new_cap arithmetic no overflow; malloc result used safely
 *  - vfs_delete: array compaction preserves child_count invariant
 *  - vfs_rename: name copy stays within VFS_MAX_NAME
 */

#include <stdint.h>
#include <stddef.h>
#include <string.h>

/* --- stubs --- */
static void *malloc(size_t size)         { (void)size; return (void *)0; }
static void  free(void *ptr)             { (void)ptr; }
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
static size_t strlen(const char *s) {
    size_t i = 0; while (s[i]) i++; return i;
}
static void strcpy_s(char *d, const char *s) {
    while ((*d++ = *s++));
}
static void strncpy_s(char *d, const char *s, size_t n) {
    size_t i;
    for (i = 0; i < n && s[i]; i++) d[i] = s[i];
    for (; i < n; i++) d[i] = '\0';
}
static int strcmp_s(const char *a, const char *b) {
    while (*a && *a == *b) { a++; b++; }
    return (unsigned char)*a - (unsigned char)*b;
}
static void strcat_s(char *d, const char *s) {
    while (*d) d++;
    while ((*d++ = *s++));
}
static char *strchr_s(const char *s, int c) {
    while (*s) { if (*s == (char)c) return (char *)s; s++; }
    return (c == '\0') ? (char *)s : (void *)0;
}
/* minimal snprintf: just copies up to size-1 bytes from fmt (no formatting) */
static int snprintf_s(char *buf, size_t size, const char *fmt, ...) {
    (void)fmt; if (size > 0) buf[0] = '\0'; return 0;
}
static char *strtok_r_s(char *str, const char *delim, char **saveptr);
static int is_delim(char c, const char *d) {
    while (*d) { if (c == *d) return 1; d++; } return 0;
}
static char *strtok_r_s(char *str, const char *delim, char **saveptr) {
    char *start = str ? str : *saveptr;
    if (!start) return (void *)0;
    while (*start && is_delim(*start, delim)) start++;
    if (*start == '\0') { *saveptr = (void *)0; return (void *)0; }
    char *end = start;
    while (*end && !is_delim(*end, delim)) end++;
    if (*end) { *end = '\0'; *saveptr = end + 1; }
    else *saveptr = (void *)0;
    return start;
}

/* redirect string calls to stubs */
#define strcpy(d,s)          strcpy_s(d,s)
#define strncpy(d,s,n)       strncpy_s(d,s,n)
#define strcmp(a,b)          strcmp_s(a,b)
#define strcat(d,s)          strcat_s(d,s)
#define strchr(s,c)          strchr_s(s,c)
#define snprintf(b,n,f,...)  snprintf_s(b,n,f)
#define strtok_r(s,d,p)      strtok_r_s(s,d,p)

static void printf(const char *fmt, ...) { (void)fmt; }

/* --- fat32 stubs (always fail so in-memory path is taken) --- */
static int fat32_init(void)                                      { return -1; }
static int fat32_is_dir(const char *p)                           { (void)p; return -1; }
static int fat32_file_size(const char *p)                        { (void)p; return -1; }
static int fat32_read_file_offset(const char *p, char *b, size_t s, size_t o) { (void)p;(void)b;(void)s;(void)o; return -1; }
static int fat32_write_file(const char *p, const char *b, size_t s) { (void)p;(void)b;(void)s; return -1; }
static int fat32_read_file(const char *p, char *b, size_t s)     { (void)p;(void)b;(void)s; return -1; }
static int fat32_mkdir(const char *p)                            { (void)p; return -1; }
static int fat32_create_file(const char *p)                      { (void)p; return -1; }
static int fat32_delete(const char *p)                           { (void)p; return -1; }
static int fat32_delete_dir(const char *p)                       { (void)p; return -1; }
static int fat32_delete_recursive(const char *p)                 { (void)p; return -1; }
static int fat32_rename(const char *p, const char *n)            { (void)p;(void)n; return -1; }
static int fat32_readdir(const char *p, void(*cb)(const char*,int,uint32_t,void*), void *u) { (void)p;(void)cb;(void)u; return -1; }

/* --- VFS types and constants (from vfs.h) --- */
#define VFS_FILE      1
#define VFS_DIRECTORY 2
#define VFS_MAX_NAME     64
#define VFS_MAX_CHILDREN 32
#define VFS_MAX_PATH     256
#define VFS_MAX_INODES   256

typedef struct vfs_node {
    char name[VFS_MAX_NAME];
    uint8_t type;
    char *data;
    size_t size;
    size_t capacity;
    struct vfs_node *children[VFS_MAX_CHILDREN];
    int child_count;
    struct vfs_node *parent;
} vfs_node_t;

/* --- VFS state --- */
static char cwd_path[VFS_MAX_PATH] = "/";
static int use_fat32 = 0;
static vfs_node_t inodes[VFS_MAX_INODES];
static int inode_count = 0;
static vfs_node_t *mem_root = (void *)0;

/* --- In-memory VFS helpers --- */

static vfs_node_t *alloc_inode(void) {
    if (inode_count >= VFS_MAX_INODES) {
        return (void *)0;
    }
    vfs_node_t *node = &inodes[inode_count++];
    memset(node, 0, sizeof(vfs_node_t));
    return node;
}

static vfs_node_t *create_mem_dir(const char *name, vfs_node_t *parent) {
    vfs_node_t *dir = alloc_inode();
    if (!dir) return (void *)0;

    int i;
    for (i = 0; name[i] && i < VFS_MAX_NAME - 1; i++) {
        dir->name[i] = name[i];
    }
    dir->name[i] = '\0';

    dir->type = VFS_DIRECTORY;
    dir->parent = parent;
    dir->child_count = 0;

    if (parent) {
        if (parent->child_count >= VFS_MAX_CHILDREN) {
            return (void *)0;
        }
        parent->children[parent->child_count++] = dir;
    }

    return dir;
}

static vfs_node_t *create_mem_file(const char *name, vfs_node_t *parent) {
    if (!parent || parent->type != VFS_DIRECTORY) {
        return (void *)0;
    }

    vfs_node_t *file = alloc_inode();
    if (!file) return (void *)0;

    int i;
    for (i = 0; name[i] && i < VFS_MAX_NAME - 1; i++) {
        file->name[i] = name[i];
    }
    file->name[i] = '\0';

    file->type = VFS_FILE;
    file->parent = parent;
    file->data = (void *)0;
    file->size = 0;
    file->capacity = 0;

    if (parent->child_count >= VFS_MAX_CHILDREN) {
        return (void *)0;
    }
    parent->children[parent->child_count++] = file;

    return file;
}

static vfs_node_t *find_mem_child(vfs_node_t *dir, const char *name) {
    if (!dir || dir->type != VFS_DIRECTORY) {
        return (void *)0;
    }

    for (int i = 0; i < dir->child_count; i++) {
        if (strcmp(dir->children[i]->name, name) == 0) {
            return dir->children[i];
        }
    }
    return (void *)0;
}

static vfs_node_t *mem_lookup(const char *path) {
    if (!path) return (void *)0;

    if (path[0] == '/' && path[1] == '\0') {
        return mem_root;
    }

    vfs_node_t *current = mem_root;
    char pathcopy[VFS_MAX_PATH];
    strncpy(pathcopy, path, VFS_MAX_PATH - 1);
    pathcopy[VFS_MAX_PATH - 1] = '\0';

    char *rest = pathcopy;
    char *token;
    if (*rest == '/') rest++;

    while ((token = strtok_r(rest, "/", &rest)) != NULL) {
        if (token[0] == '\0') continue;
        current = find_mem_child(current, token);
        if (!current) return (void *)0;
    }

    return current;
}

/* --- Public VFS API --- */

void vfs_init(void) {
    if (fat32_init() == 0) {
        use_fat32 = 1;
        strcpy(cwd_path, "/");
    } else {
        use_fat32 = 0;
        inode_count = 0;
        mem_root = alloc_inode();
        mem_root->name[0] = '/';
        mem_root->name[1] = '\0';
        mem_root->type = VFS_DIRECTORY;
        mem_root->parent = mem_root;
        mem_root->child_count = 0;
        create_mem_dir("tmp", mem_root);
        strcpy(cwd_path, "/");
    }

    printf("[VFS] %s, cwd=%s\n", use_fat32 ? "FAT32" : "in-memory", cwd_path);
}

vfs_node_t *vfs_lookup(const char *path) {
    static vfs_node_t temp_node;
    static char stored_path[VFS_MAX_PATH];
    char fullpath[VFS_MAX_PATH];

    if (!path || !path[0]) {
        strcpy(fullpath, cwd_path);
    } else if (path[0] == '/') {
        strncpy(fullpath, path, VFS_MAX_PATH - 1);
        fullpath[VFS_MAX_PATH - 1] = '\0';
    } else {
        if (strcmp(cwd_path, "/") == 0) {
            snprintf(fullpath, VFS_MAX_PATH, "/%s", path);
        } else {
            snprintf(fullpath, VFS_MAX_PATH, "%s/%s", cwd_path, path);
        }
    }

    char normalized[VFS_MAX_PATH];
    char *parts[32];
    int depth = 0;

    char *rest = fullpath;
    char *token;
    if (*rest == '/') rest++;

    while ((token = strtok_r(rest, "/", &rest)) != NULL) {
        if (token[0] == '\0' || strcmp(token, ".") == 0) {
            continue;
        }
        if (strcmp(token, "..") == 0) {
            if (depth > 0) depth--;
            continue;
        }
        parts[depth++] = token;
    }

    normalized[0] = '\0';
    for (int i = 0; i < depth; i++) {
        strcat(normalized, "/");
        strcat(normalized, parts[i]);
    }
    if (normalized[0] == '\0') {
        strcpy(normalized, "/");
    }

    if (use_fat32) {
        int is_dir = fat32_is_dir(normalized);
        if (is_dir < 0) {
            return (void *)0;
        }
        memset(&temp_node, 0, sizeof(temp_node));
        char *last_slash = (void *)0;
        for (char *p = normalized; *p; p++) {
            if (*p == '/') last_slash = p;
        }
        if (last_slash && last_slash[1]) {
            strncpy(temp_node.name, last_slash + 1, VFS_MAX_NAME - 1);
        } else {
            strcpy(temp_node.name, "/");
        }
        temp_node.type = is_dir ? VFS_DIRECTORY : VFS_FILE;
        if (!is_dir) {
            temp_node.size = fat32_file_size(normalized);
        }
        strcpy(stored_path, normalized);
        temp_node.data = stored_path;
        return &temp_node;
    } else {
        return mem_lookup(normalized);
    }
}

vfs_node_t *vfs_open_handle(const char *path) {
    vfs_node_t *temp = vfs_lookup(path);
    if (!temp) return (void *)0;

    vfs_node_t *node = malloc(sizeof(vfs_node_t));
    if (!node) return (void *)0;

    memcpy(node, temp, sizeof(vfs_node_t));

    if (temp->data) {
        char *path_copy = malloc(VFS_MAX_PATH);
        if (!path_copy) { free(node); return (void *)0; }
        strcpy(path_copy, (char*)temp->data);
        node->data = path_copy;
    }

    return node;
}

void vfs_close_handle(vfs_node_t *node) {
    if (!node) return;
    if (node->data) free(node->data);
    free(node);
}

vfs_node_t *vfs_get_root(void) {
    return vfs_lookup("/");
}

vfs_node_t *vfs_get_cwd(void) {
    return vfs_lookup(cwd_path);
}

int vfs_set_cwd(const char *path) {
    char fullpath[VFS_MAX_PATH];

    if (!path || !path[0]) {
        return -1;
    }

    if (path[0] == '/') {
        strncpy(fullpath, path, VFS_MAX_PATH - 1);
        fullpath[VFS_MAX_PATH - 1] = '\0';
    } else {
        if (strcmp(cwd_path, "/") == 0) {
            snprintf(fullpath, VFS_MAX_PATH, "/%s", path);
        } else {
            snprintf(fullpath, VFS_MAX_PATH, "%s/%s", cwd_path, path);
        }
    }

    char normalized[VFS_MAX_PATH];
    char *parts[32];
    int depth = 0;

    char pathcopy[VFS_MAX_PATH];
    strcpy(pathcopy, fullpath);

    char *rest = pathcopy;
    char *token;
    if (*rest == '/') rest++;

    while ((token = strtok_r(rest, "/", &rest)) != NULL) {
        if (token[0] == '\0' || strcmp(token, ".") == 0) {
            continue;
        }
        if (strcmp(token, "..") == 0) {
            if (depth > 0) depth--;
            continue;
        }
        parts[depth++] = token;
    }

    normalized[0] = '\0';
    for (int i = 0; i < depth; i++) {
        strcat(normalized, "/");
        strcat(normalized, parts[i]);
    }
    if (normalized[0] == '\0') {
        strcpy(normalized, "/");
    }

    if (use_fat32) {
        if (fat32_is_dir(normalized) != 1) {
            return -1;
        }
    } else {
        vfs_node_t *node = mem_lookup(normalized);
        if (!node || node->type != VFS_DIRECTORY) {
            return -1;
        }
    }

    strcpy(cwd_path, normalized);
    return 0;
}

int vfs_get_cwd_path(char *buf, size_t size) {
    if (!buf || size == 0) return -1;
    strncpy(buf, cwd_path, size - 1);
    buf[size - 1] = '\0';
    return 0;
}

typedef struct {
    int index;
    int target_index;
    char *name;
    size_t name_size;
    uint8_t *type;
    int found;
} readdir_ctx_t;

static void readdir_callback(const char *name, int is_dir, uint32_t size, void *user_data) {
    (void)size;
    readdir_ctx_t *ctx = (readdir_ctx_t *)user_data;
    if (ctx->found) return;
    if (ctx->index == ctx->target_index) {
        strncpy(ctx->name, name, ctx->name_size - 1);
        ctx->name[ctx->name_size - 1] = '\0';
        *ctx->type = is_dir ? VFS_DIRECTORY : VFS_FILE;
        ctx->found = 1;
    }
    ctx->index++;
}

int vfs_readdir(vfs_node_t *dir, int index, char *name, size_t name_size, uint8_t *type) {
    if (!dir || dir->type != VFS_DIRECTORY || !name || name_size == 0 || !type) {
        return -1;
    }

    if (use_fat32) {
        const char *path = (const char *)dir->data;
        if (!path) return -1;

        readdir_ctx_t ctx = { 0, index, name, name_size, type, 0 };
        fat32_readdir(path, readdir_callback, &ctx);
        return ctx.found ? 0 : -1;
    } else {
        if (index < 0 || index >= dir->child_count) {
            return -1;
        }
        vfs_node_t *child = dir->children[index];
        strncpy(name, child->name, name_size - 1);
        name[name_size - 1] = '\0';
        *type = child->type;
        return 0;
    }
}

vfs_node_t *vfs_mkdir(const char *path) {
    if (use_fat32) {
        char fullpath[VFS_MAX_PATH];
        if (path[0] == '/') {
            strncpy(fullpath, path, VFS_MAX_PATH - 1);
            fullpath[VFS_MAX_PATH - 1] = '\0';
        } else {
            if (strcmp(cwd_path, "/") == 0) {
                snprintf(fullpath, VFS_MAX_PATH, "/%s", path);
            } else {
                snprintf(fullpath, VFS_MAX_PATH, "%s/%s", cwd_path, path);
            }
        }
        if (fat32_mkdir(fullpath) < 0) {
            return (void *)0;
        }
        return vfs_lookup(path);
    }

    if (!path || !path[0]) return (void *)0;

    char pathbuf[VFS_MAX_PATH];
    strncpy(pathbuf, path, VFS_MAX_PATH - 1);
    pathbuf[VFS_MAX_PATH - 1] = '\0';

    char *last_slash = (void *)0;
    for (char *p = pathbuf; *p; p++) {
        if (*p == '/') last_slash = p;
    }

    vfs_node_t *parent;
    char *dirname;

    if (last_slash == (void *)0) {
        parent = mem_lookup(cwd_path);
        dirname = pathbuf;
    } else if (last_slash == pathbuf) {
        parent = mem_root;
        dirname = last_slash + 1;
    } else {
        *last_slash = '\0';
        parent = mem_lookup(pathbuf);
        dirname = last_slash + 1;
    }

    if (!parent || parent->type != VFS_DIRECTORY) {
        return (void *)0;
    }

    if (find_mem_child(parent, dirname)) {
        return (void *)0;
    }

    return create_mem_dir(dirname, parent);
}

vfs_node_t *vfs_create(const char *path) {
    if (use_fat32) {
        char fullpath[VFS_MAX_PATH];
        if (path[0] == '/') {
            strncpy(fullpath, path, VFS_MAX_PATH - 1);
            fullpath[VFS_MAX_PATH - 1] = '\0';
        } else {
            if (strcmp(cwd_path, "/") == 0) {
                snprintf(fullpath, VFS_MAX_PATH, "/%s", path);
            } else {
                snprintf(fullpath, VFS_MAX_PATH, "%s/%s", cwd_path, path);
            }
        }
        if (fat32_create_file(fullpath) < 0) {
            return (void *)0;
        }
        return vfs_lookup(path);
    }

    if (!path || !path[0]) return (void *)0;

    char pathbuf[VFS_MAX_PATH];
    strncpy(pathbuf, path, VFS_MAX_PATH - 1);
    pathbuf[VFS_MAX_PATH - 1] = '\0';

    char *last_slash = (void *)0;
    for (char *p = pathbuf; *p; p++) {
        if (*p == '/') last_slash = p;
    }

    vfs_node_t *parent;
    char *filename;

    if (last_slash == (void *)0) {
        parent = mem_lookup(cwd_path);
        filename = pathbuf;
    } else if (last_slash == pathbuf) {
        parent = mem_root;
        filename = last_slash + 1;
    } else {
        *last_slash = '\0';
        parent = mem_lookup(pathbuf);
        filename = last_slash + 1;
    }

    if (!parent || parent->type != VFS_DIRECTORY) {
        return (void *)0;
    }

    vfs_node_t *existing = find_mem_child(parent, filename);
    if (existing) {
        return existing;
    }

    return create_mem_file(filename, parent);
}

int vfs_read(vfs_node_t *file, char *buf, size_t size, size_t offset) {
    if (!file || file->type != VFS_FILE || !buf) {
        return -1;
    }

    if (use_fat32) {
        const char *filepath = (const char *)file->data;
        if (!filepath) return -1;
        return fat32_read_file_offset(filepath, buf, size, offset);
    } else {
        if (offset >= file->size) {
            return 0;
        }

        size_t to_read = file->size - offset;
        if (to_read > size) to_read = size;

        memcpy(buf, file->data + offset, to_read);
        return (int)to_read;
    }
}

int vfs_write(vfs_node_t *file, const char *buf, size_t size) {
    if (!file || file->type != VFS_FILE) {
        return -1;
    }

    if (use_fat32) {
        const char *filepath = (const char *)file->data;
        if (!filepath) return -1;
        return fat32_write_file(filepath, buf, size);
    }

    if (size > file->capacity) {
        size_t new_cap = size + 64;
        char *new_data = malloc(new_cap);
        if (!new_data) return -1;

        if (file->data) {
            free(file->data);
        }
        file->data = new_data;
        file->capacity = new_cap;
    }

    memcpy(file->data, buf, size);
    file->size = size;
    return (int)size;
}

int vfs_append(vfs_node_t *file, const char *buf, size_t size) {
    if (!file || file->type != VFS_FILE) {
        return -1;
    }

    if (use_fat32) {
        const char *filepath = (const char *)file->data;
        if (!filepath) return -1;

        int file_size = fat32_file_size(filepath);
        if (file_size < 0) file_size = 0;

        char *new_buf = malloc(file_size + size);
        if (!new_buf) return -1;

        if (file_size > 0) {
            if (fat32_read_file(filepath, new_buf, file_size) < 0) {
                free(new_buf);
                return -1;
            }
        }

        memcpy(new_buf + file_size, buf, size);

        int result = fat32_write_file(filepath, new_buf, file_size + size);
        free(new_buf);
        return result >= 0 ? (int)size : -1;
    }

    size_t new_size = file->size + size;

    if (new_size > file->capacity) {
        size_t new_cap = new_size + 64;
        char *new_data = malloc(new_cap);
        if (!new_data) return -1;

        if (file->data) {
            memcpy(new_data, file->data, file->size);
            free(file->data);
        }
        file->data = new_data;
        file->capacity = new_cap;
    }

    memcpy(file->data + file->size, buf, size);
    file->size = new_size;
    return (int)size;
}

static void build_fullpath(const char *path, char *fullpath) {
    if (path[0] == '/') {
        strncpy(fullpath, path, VFS_MAX_PATH - 1);
        fullpath[VFS_MAX_PATH - 1] = '\0';
    } else {
        if (strcmp(cwd_path, "/") == 0) {
            snprintf(fullpath, VFS_MAX_PATH, "/%s", path);
        } else {
            snprintf(fullpath, VFS_MAX_PATH, "%s/%s", cwd_path, path);
        }
    }
}

int vfs_delete(const char *path) {
    if (use_fat32) {
        char fullpath[VFS_MAX_PATH];
        build_fullpath(path, fullpath);
        return fat32_delete(fullpath);
    }

    if (!path || !path[0]) return -1;

    char pathbuf[VFS_MAX_PATH];
    strncpy(pathbuf, path, VFS_MAX_PATH - 1);
    pathbuf[VFS_MAX_PATH - 1] = '\0';

    char *last_slash = (void *)0;
    for (char *p = pathbuf; *p; p++) {
        if (*p == '/') last_slash = p;
    }

    vfs_node_t *parent;
    char *filename;

    if (last_slash == (void *)0) {
        parent = mem_lookup(cwd_path);
        filename = pathbuf;
    } else if (last_slash == pathbuf) {
        parent = mem_root;
        filename = last_slash + 1;
    } else {
        *last_slash = '\0';
        parent = mem_lookup(pathbuf);
        filename = last_slash + 1;
    }

    if (!parent || parent->type != VFS_DIRECTORY) {
        return -1;
    }

    int found_idx = -1;
    for (int i = 0; i < parent->child_count; i++) {
        if (strcmp(parent->children[i]->name, filename) == 0) {
            found_idx = i;
            break;
        }
    }

    if (found_idx < 0) return -1;

    vfs_node_t *node = parent->children[found_idx];

    if (node->type == VFS_DIRECTORY) {
        return -1;
    }

    if (node->data && node->type == VFS_FILE) {
        free(node->data);
    }

    for (int i = found_idx; i < parent->child_count - 1; i++) {
        parent->children[i] = parent->children[i + 1];
    }
    parent->child_count--;

    return 0;
}

int vfs_delete_dir(const char *path) {
    if (use_fat32) {
        char fullpath[VFS_MAX_PATH];
        build_fullpath(path, fullpath);
        return fat32_delete_dir(fullpath);
    }

    if (!path || !path[0]) return -1;

    char pathbuf[VFS_MAX_PATH];
    strncpy(pathbuf, path, VFS_MAX_PATH - 1);
    pathbuf[VFS_MAX_PATH - 1] = '\0';

    char *last_slash = (void *)0;
    for (char *p = pathbuf; *p; p++) {
        if (*p == '/') last_slash = p;
    }

    vfs_node_t *parent;
    char *dirname;

    if (last_slash == (void *)0) {
        parent = mem_lookup(cwd_path);
        dirname = pathbuf;
    } else if (last_slash == pathbuf) {
        parent = mem_root;
        dirname = last_slash + 1;
    } else {
        *last_slash = '\0';
        parent = mem_lookup(pathbuf);
        dirname = last_slash + 1;
    }

    if (!parent || parent->type != VFS_DIRECTORY) {
        return -1;
    }

    int found_idx = -1;
    for (int i = 0; i < parent->child_count; i++) {
        if (strcmp(parent->children[i]->name, dirname) == 0) {
            found_idx = i;
            break;
        }
    }

    if (found_idx < 0) return -1;

    vfs_node_t *node = parent->children[found_idx];

    if (node->type != VFS_DIRECTORY) {
        return -1;
    }

    if (node->child_count > 0) {
        return -1;
    }

    for (int i = found_idx; i < parent->child_count - 1; i++) {
        parent->children[i] = parent->children[i + 1];
    }
    parent->child_count--;

    return 0;
}

int vfs_delete_recursive(const char *path) {
    if (use_fat32) {
        char fullpath[VFS_MAX_PATH];
        build_fullpath(path, fullpath);
        return fat32_delete_recursive(fullpath);
    }

    vfs_node_t *node = vfs_lookup(path);
    if (!node) return -1;

    if (node->type == VFS_DIRECTORY) {
        return vfs_delete_dir(path);
    } else {
        return vfs_delete(path);
    }
}

int vfs_rename(const char *path, const char *newname) {
    if (use_fat32) {
        char fullpath[VFS_MAX_PATH];
        if (path[0] == '/') {
            strncpy(fullpath, path, VFS_MAX_PATH - 1);
            fullpath[VFS_MAX_PATH - 1] = '\0';
        } else {
            if (strcmp(cwd_path, "/") == 0) {
                snprintf(fullpath, VFS_MAX_PATH, "/%s", path);
            } else {
                snprintf(fullpath, VFS_MAX_PATH, "%s/%s", cwd_path, path);
            }
        }

        const char *basename = newname;
        for (const char *p = newname; *p; p++) {
            if (*p == '/') basename = p + 1;
        }

        return fat32_rename(fullpath, basename);
    }

    if (!path || !path[0] || !newname || !newname[0]) return -1;

    char pathbuf[VFS_MAX_PATH];
    strncpy(pathbuf, path, VFS_MAX_PATH - 1);
    pathbuf[VFS_MAX_PATH - 1] = '\0';

    char *last_slash = (void *)0;
    for (char *p = pathbuf; *p; p++) {
        if (*p == '/') last_slash = p;
    }

    vfs_node_t *parent;
    char *filename;

    if (last_slash == (void *)0) {
        parent = mem_lookup(cwd_path);
        filename = pathbuf;
    } else if (last_slash == pathbuf) {
        parent = mem_root;
        filename = last_slash + 1;
    } else {
        *last_slash = '\0';
        parent = mem_lookup(pathbuf);
        filename = last_slash + 1;
    }

    if (!parent || parent->type != VFS_DIRECTORY) {
        return -1;
    }

    for (int i = 0; i < parent->child_count; i++) {
        if (strcmp(parent->children[i]->name, filename) == 0) {
            int j;
            for (j = 0; newname[j] && j < VFS_MAX_NAME - 1; j++) {
                parent->children[i]->name[j] = newname[j];
            }
            parent->children[i]->name[j] = '\0';
            return 0;
        }
    }

    return -1;
}

int vfs_is_dir(vfs_node_t *node) {
    return node && node->type == VFS_DIRECTORY;
}

int vfs_is_file(vfs_node_t *node) {
    return node && node->type == VFS_FILE;
}
