/*
 * lv_conf.h — simulator configuration for LVGL 9.5 (matches the firmware
 * feature set the EEZ export relies on: default theme, a few built-in
 * montserrat faces for the fonts[] table, SW render, no OS).
 * If a project needs more built-ins, build_sim.py appends them below via
 * the SIM_EXTRA_MONTSERRAT define list.
 */
#ifndef LV_CONF_H
#define LV_CONF_H

#define LV_COLOR_DEPTH 32

#define LV_MEM_CUSTOM 0
#define LV_MEM_SIZE (1024U * 1024U)

/* tick via lv_tick_inc() from the sim loop */
#define LV_TICK_CUSTOM 0

#define LV_USE_OS LV_OS_NONE
#define LV_USE_STDLIB_MALLOC 1
#define LV_USE_CLIB 1
#define LV_USE_SDL 0

/* built-in fonts referenced by the generated fonts[] table */
#define LV_FONT_MONTSERRAT_12 1
#define LV_FONT_MONTSERRAT_14 1
#define LV_FONT_MONTSERRAT_16 1
#define LV_FONT_MONTSERRAT_18 1
#define LV_FONT_MONTSERRAT_20 1
#define LV_FONT_MONTSERRAT_22 1
#define LV_FONT_MONTSERRAT_24 1
#define LV_FONT_MONTSERRAT_26 1
#define LV_FONT_MONTSERRAT_28 1
#define LV_FONT_MONTSERRAT_30 1
#define LV_FONT_MONTSERRAT_32 1
#define LV_FONT_MONTSERRAT_34 1
#define LV_FONT_MONTSERRAT_36 1
#define LV_FONT_MONTSERRAT_38 1
#define LV_FONT_MONTSERRAT_40 1
#define LV_FONT_MONTSERRAT_42 1
#define LV_FONT_MONTSERRAT_44 1
#define LV_FONT_MONTSERRAT_46 1
#define LV_FONT_MONTSERRAT_48 1
#define LV_FONT_DEFAULT &lv_font_montserrat_14

/* widgets on — the generated screens.c creates all core types */
#define LV_USE_WIDGETS 1

#define LV_USE_THEME_DEFAULT 1
#define LV_THEME_DEFAULT_DARK 1

#define LV_USE_DRAW_SW 1
#define LV_DRAW_SW_COMPLEX 1

#define LV_USE_FLEX 1
#define LV_USE_GRID 1
#define LV_USE_SNAPSHOT 0
#define LV_USE_CHART 1
#define LV_USE_TABLE 1
#define LV_USE_CALENDAR 1
#define LV_USE_KEYBOARD 1
#define LV_USE_SPINBOX 1
#define LV_USE_ROLLER 1
#define LV_USE_TEXTAREA 1
#define LV_USE_TABVIEW 1
#define LV_USE_CANVAS 1
#define LV_USE_SCALE 1
#define LV_USE_SPINNER 1
#define LV_USE_LED 1
#define LV_USE_ARC 1
#define LV_USE_BAR 1
#define LV_USE_BUTTON 1
#define LV_USE_BUTTONMATRIX 1
#define LV_USE_CHECKBOX 1
#define LV_USE_DROPDOWN 1
#define LV_USE_IMAGE 1
#define LV_USE_LABEL 1
#define LV_USE_LINE 1
#define LV_USE_SLIDER 1
#define LV_USE_SWITCH 1
#define LV_USE_ANIMIMG 1
#define LV_USE_IMAGEBUTTON 0
#define LV_USE_LIST 1
#define LV_USE_MENU 1
#define LV_USE_MSGBOX 1
#define LV_USE_TILEVIEW 1
#define LV_USE_WIN 1
#define LV_USE_SPAN 1
#define LV_USE_GIF 0
#define LV_USE_IMGFONT 0
#define LV_USE_FREETYPE 0
#define LV_USE_TINY_TTF 0
#define LV_USE_THORVE_INTERNAL 0
#define LV_USE_LODEPNG 0
#define LV_USE_LIBPNG 0
#define LV_USE_BMP 0
#define LV_USE_TJPGD 0
#define LV_USE_FS_MEMFS 0

#define LV_USE_EXAMPLES 0
#define LV_USE_DEMO_WIDGETS 0
#define LV_USE_DEMO_STRESS 0
#define LV_USE_DEMO_TRANSFORM 0
#define LV_USE_DEMO_SCROLL 0
#define LV_USE_DEMO_ENCG 0
#define LV_USE_DEMO_MUSIC 0
#define LV_USE_DEMO_BENCHMARK 0
#define LV_USE_DEMO_KEYPAD_AND_ENCODER 0
#define LV_USE_DEMO_FLEX_LAYOUT 0
#define LV_USE_DEMO_MULTILANG 0
#define LV_USE_DEMO_VECTOR_GRAPHIC 0

#endif /* LV_CONF_H */
