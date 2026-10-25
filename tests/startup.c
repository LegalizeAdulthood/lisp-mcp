/* Disposable VLM process with a bounded v1 TCP peer; no Lisp evaluator. */
#define main snapshot_main
#include "detached.c"
#undef main
#include <arpa/inet.h>
#include <errno.h>
#include <pthread.h>
#include <stddef.h>
#include <stdint.h>
#include <string.h>
#include <sys/socket.h>

static char world[4096];

static int receive_bytes(int fd, char *data, size_t size)
{
    while (size) {
        ssize_t count = recv(fd, data, size, 0);
        if (count <= 0) return 0;
        data += count;
        size -= (size_t)count;
    }
    return 1;
}

static void *client(void *argument)
{
    int fd = (int)(intptr_t)argument;
    for (;;) {
        char ch = 0, request[16385], response[16385], id[512];
        size_t size = 0;
        while (receive_bytes(fd, &ch, 1) && ch >= '0' && ch <= '9') {
            size = size * 10 + (unsigned)(ch - '0');
            if (size > 16384) goto done;
        }
        if (ch != ' ' || !size || !receive_bytes(fd, request, size)) break;
        request[size] = 0;
        char *start = strstr(request, ":id \"");
        if (!start) break;
        start += 5;
        char *end = strchr(start, '"');
        if (!end || end - start >= (ptrdiff_t)sizeof id) break;
        memcpy(id, start, (size_t)(end - start));
        id[end - start] = 0;
        int count;
        if (strstr(request, ":op :eval")) {
            count = snprintf(response, sizeof response,
                "(:version 1 :id \"%s\" :ok t :boot-id \"boot-%d\" :service-id \"service-%d\" "
                ":request-state :completed :values (\"42\") :value-count 1 :stdout \"\" :stderr \"\" "
                ":warnings nil :package \"USER\" :elapsed-ms 1 :truncated nil :replaced nil)", id, getpid(), getpid());
        } else {
            count = snprintf(response, sizeof response,
                "(:version 1 :id \"%s\" :ok t :boot-id \"boot-%d\" :service-id \"service-%d\" "
                ":world \"%s\" :package \"USER\" :busy nil :features (:status :request-status :eval))",
                id, getpid(), getpid(), world);
        }
        char header[32];
        int length = snprintf(header, sizeof header, "%d ", count);
        if (send(fd, header, (size_t)length, MSG_NOSIGNAL) != length ||
            send(fd, response, (size_t)count, MSG_NOSIGNAL) != count) break;
    }
done:
    close(fd);
    return NULL;
}

int main(int argc, char **argv)
{
    for (int i = 1; i + 1 < argc; ++i) {
        if (!strcmp(argv[i], "-world")) {
            char *name = strrchr(argv[i + 1], '/');
            snprintf(world, sizeof world, "%s", name ? name + 1 : argv[i + 1]);
        }
    }
    int code = snapshot_main(argc, argv);
    if (code) return code;
    if (getenv("VLM_FIXTURE_NO_LISTENER")) {
        sleep(30);
        return 0;
    }
    int listener = socket(AF_INET, SOCK_STREAM, 0), reuse = 1;
    setsockopt(listener, SOL_SOCKET, SO_REUSEADDR, &reuse, sizeof reuse);
    struct sockaddr_in address = {.sin_family = AF_INET, .sin_port = htons(8888)};
    inet_pton(AF_INET, "127.0.0.2", &address.sin_addr);
    if (bind(listener, (struct sockaddr *)&address, sizeof address) || listen(listener, 8)) return 95;
    for (;;) {
        int fd = accept(listener, NULL, NULL);
        if (fd < 0) {
            if (errno == EINTR) continue;
            return 96;
        }
        pthread_t thread;
        if (pthread_create(&thread, NULL, client, (void *)(intptr_t)fd)) return 97;
        pthread_detach(thread);
    }
}
