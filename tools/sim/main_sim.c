/*
 * main_sim.c — LVGL firmware simulator shell for emscripten.
 *
 * Runs the REAL firmware sources (screens.c / flow_def.c / eez-flow.cpp —
 * the same code a device builds) inside the browser:
 *   - full-frame SW render into an RGBA buffer, blitted to a canvas
 *   - mouse pointer indev wired to LVGL (clicks drive flows)
 *   - 33ms tick: lv_tick_inc + lv_timer_handler + ui_tick
 *
 * W/H are injected via -DSIM_W / -DSIM_H by tools/build_sim.py.
 * 模拟器壳：固件同一份代码跑在浏览器里（画布渲染 + 鼠标 + 心跳）。
 */
#include <stdio.h>
#include <string.h>
#include <emscripten/html5.h>
#include "lvgl/lvgl.h"
#include "ui.h"

#ifndef SIM_W
#define SIM_W 480
#endif
#ifndef SIM_H
#define SIM_H 320
#endif

static lv_display_t *g_disp;
static lv_indev_t *g_mouse;
static int g_last_x, g_last_y;
static bool g_down;

static void sim_flush(lv_display_t *disp, const lv_area_t *area, uint8_t *px_map)
{
    (void)area;
    int32_t w = lv_display_get_horizontal_resolution(disp);
    int32_t h = lv_display_get_vertical_resolution(disp);
    /* px_map is the full-frame ARGB8888 buffer (render mode FULL) */
    EM_ASM_({
        var img = Module.simImage;
        if (!img || typeof Module.simBlit !== 'function') return;
        Module.simBlit(HEAPU8.slice($0, $0 + $1 * $2 * 4), $1, $2);
    }, (int)px_map, (int)w, (int)h);
    lv_display_flush_ready(disp);
}

static void sim_mouse_read(lv_indev_t *indev, lv_indev_data_t *data)
{
    (void)indev;
    data->point.x = g_last_x;
    data->point.y = g_last_y;
    data->state = g_down ? LV_INDEV_STATE_PRESSED : LV_INDEV_STATE_RELEASED;
}

static EM_BOOL on_mouse(int type, const EmscriptenMouseEvent *e, void *ud)
{
    (void)ud;
    double cw, ch;
    emscripten_get_element_css_size("#canvas", &cw, &ch);
    if (cw > 0 && ch > 0) {
        g_last_x = (int)(e->targetX * SIM_W / cw);
        g_last_y = (int)(e->targetY * SIM_H / ch);
    } else {
        g_last_x = e->targetX;
        g_last_y = e->targetY;
    }
    g_down = (type == EMSCRIPTEN_EVENT_MOUSEDOWN ||
              (type == EMSCRIPTEN_EVENT_MOUSEMOVE && e->buttons & 1));
    return EM_TRUE;
}


static void sim_loop(void)
{
    lv_tick_inc(33);
    lv_timer_handler();
    ui_tick();
}

int main(void)
{
    lv_init();
    g_disp = lv_display_create(SIM_W, SIM_H);
    lv_display_set_flush_cb(g_disp, sim_flush);
    lv_display_set_color_format(g_disp, LV_COLOR_FORMAT_ARGB8888);
    static uint8_t buf1[SIM_W * SIM_H * 4];
    lv_display_set_buffers(g_disp, buf1, NULL, sizeof(buf1), LV_DISPLAY_RENDER_MODE_FULL);

    g_mouse = lv_indev_create();
    lv_indev_set_type(g_mouse, LV_INDEV_TYPE_POINTER);
    lv_indev_set_read_cb(g_mouse, sim_mouse_read);
    emscripten_set_mousedown_callback("#canvas", NULL, 0, on_mouse);
    emscripten_set_mouseup_callback("#canvas", NULL, 0, on_mouse);
    emscripten_set_mousemove_callback("#canvas", NULL, 0, on_mouse);

    ui_init();
    emscripten_set_main_loop(sim_loop, 30, 1);
    return 0;
}
