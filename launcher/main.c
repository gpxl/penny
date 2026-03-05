/*
 * Penny launcher — compiled native binary for proper TCC/privacy attribution.
 *
 * Replaces the bash script in Penny.app/Contents/MacOS/Penny so that macOS
 * associates the bundle with a real Mach-O binary, enabling correct display in
 * System Preferences → Privacy & Security and TCC permission dialogs.
 *
 * Strategy: stay alive as the bundle process (use fork+exec, not execv) so that
 * launchd KeepAlive tracks this process, while the Python child does the work.
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <libgen.h>
#include <sys/wait.h>

/* Python interpreter to launch */
static const char *PYTHON = "/opt/homebrew/bin/python3.11";

int main(int argc, char *argv[]) {
    (void)argc;

    /* Resolve the directory containing this binary.
     * argv[0] is an absolute path when launched by launchd. */
    char self_path[4096];
    if (realpath(argv[0], self_path) == NULL) {
        /* fallback: use argv[0] as-is */
        strncpy(self_path, argv[0], sizeof(self_path) - 1);
        self_path[sizeof(self_path) - 1] = '\0';
    }

    /* Bundle layout: Contents/MacOS/<binary>
     * penny_main.py is in the same directory as this binary. */
    char *bin_dir = dirname(self_path);

    char penny_main[4096];
    snprintf(penny_main, sizeof(penny_main), "%s/penny_main.py", bin_dir);

    /* Fork so this process stays alive for launchd KeepAlive tracking. */
    pid_t pid = fork();
    if (pid < 0) {
        perror("Penny: fork failed");
        return 1;
    }

    if (pid == 0) {
        /* Child: exec Python with penny_main.py */
        char *child_argv[] = {(char *)PYTHON, penny_main, NULL};
        execv(PYTHON, child_argv);
        perror("Penny: execv failed");
        _exit(1);
    }

    /* Parent: wait for child and propagate exit status */
    int status = 0;
    waitpid(pid, &status, 0);

    if (WIFEXITED(status)) {
        return WEXITSTATUS(status);
    }
    return 1;
}
