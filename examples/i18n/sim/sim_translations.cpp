#include <string.h>
extern "C" const char *uixml_sim_translate(const char *key) {
    if (strcmp(key, "speed") == 0) return "Speed";
    if (strcmp(key, "start") == 0) return "Start";
    if (strcmp(key, "status_ok") == 0) return "All systems nominal";
    if (strcmp(key, "stop") == 0) return "Stop";
    if (strcmp(key, "temperature") == 0) return "Temperature";
    if (strcmp(key, "title") == 0) return "Motor Controller";
    return 0;
}
