#define _GNU_SOURCE
#include <dirent.h>
#include <stdio.h>
#include <stdlib.h>
#include <unistd.h>

static void string(FILE *out, const char *text)
{
    fputc('"', out);
    if (text) {
        for (const unsigned char *p = (const unsigned char *)text; *p; ++p) {
            if (*p == '"' || *p == '\\') fputc('\\', out);
            if (*p < 32) fprintf(out, "\\u%04x", *p);
            else fputc(*p, out);
        }
    }
    fputc('"', out);
}

int main(int argc, char **argv)
{
    const char *report = getenv("VLM_FIXTURE_REPORT");
    const char *delay = getenv("VLM_FIXTURE_DELAY");
    const char *code = getenv("VLM_FIXTURE_EXIT");
    if (!report) return 90;
    FILE *out = fopen(report, "w");
    if (!out) return 91;
    char cwd[4096], target[4096], path[128], ch;
    if (!getcwd(cwd, sizeof cwd)) return 92;
    fprintf(out, "{\"pid\":%d,\"sid\":%d,\"pgid\":%d,\"cwd\":", getpid(), getsid(0), getpgrp());
    string(out, cwd);
    fprintf(out, ",\"stdin_eof\":%s,\"argv\":[", read(0, &ch, 1) == 0 ? "true" : "false");
    for (int i = 0; i < argc; ++i) {
        if (i) fputc(',', out);
        string(out, argv[i]);
    }
    fputs("],\"environment\":[", out);
    const char *names[] = {"DISPLAY", "WAYLAND_DISPLAY", "XDG_RUNTIME_DIR"};
    for (int i = 0; i < 3; ++i) {
        if (i) fputc(',', out);
        string(out, getenv(names[i]));
    }
    fputs("],\"fds\":{", out);
    DIR *directory = opendir("/proc/self/fd");
    if (!directory) return 93;
    struct dirent *entry;
    int first = 1;
    while ((entry = readdir(directory))) {
        if (entry->d_name[0] == '.') continue;
        int fd = atoi(entry->d_name);
        if (fd == dirfd(directory) || fd == fileno(out)) continue;
        snprintf(path, sizeof path, "/proc/self/fd/%d", fd);
        ssize_t size = readlink(path, target, sizeof target - 1);
        if (size < 0) return 94;
        target[size] = 0;
        if (!first) fputc(',', out);
        first = 0;
        fprintf(out, "\"%d\":", fd);
        string(out, target);
    }
    closedir(directory);
    fputs("}}\n", out);
    fclose(out);
    puts("fixture stdout before parent exit");
    fputs("fixture stderr before parent exit\n", stderr);
    fflush(NULL);
    usleep((unsigned)(delay ? atoi(delay) : 3000) * 1000);
    puts("fixture stdout after parent exit");
    fputs("fixture stderr after parent exit\n", stderr);
    fflush(NULL);
    return code ? atoi(code) : 0;
}
