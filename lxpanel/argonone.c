/*
 * (c) 2020- Spiros Papadimitriou <spapadim@gmail.com>
 *
 * This file is released under the MIT License:
 *    https://opensource.org/licenses/MIT
 * This software is distributed on an "AS IS" basis,
 * WITHOUT WARRANTY OF ANY KIND, either express or implied.
 */

#include <errno.h>
#include <locale.h>
#include <lxpanel/conf.h>
#include <stdlib.h>
#include <string.h>

#include <gio/gio.h>

#include <lxpanel/plugin.h>

#define DEBUG_ON
#ifdef DEBUG_ON
#define DEBUG(fmt,args...) g_message("DBG: " fmt,##args)
#else
#define DEBUG
#endif

/* argonone event names -- should match definitions in Python module */
#define NOTIFY_VALUE_TEMPERATURE "temperature"  /* float */
#define NOTIFY_VALUE_FAN_SPEED   "fan_speed"    /* int */
#define NOTIFY_VALUE_FAN_CONTROL_ENABLED   "fan_control_enabled"    /* bool */
#define NOTIFY_VALUE_POWER_CONTROL_ENABLED "power_control_enabled"  /* bool */


/* Plug-in global data */
typedef struct {
  LXPanel *panel;
  config_setting_t *settings;

  GtkWidget *plugin;
  GtkWidget *tray_icon;
  GtkWidget *tray_label;
  GtkWidget *popup_menu;

  GDBusProxy *proxy;
  
  /* "Model" part of UI */
  gboolean show_label;
  gboolean include_temperature;
  gint32 fan_speed;
  gdouble temperature;
  gboolean is_fan_control_enabled;
} ArgonOnePlugin;


static void argonone_update_view (ArgonOnePlugin *aone, gboolean fan_changed, gboolean config_updated);


/***********************************************
 * Plugin D-Bus connection(s)                  */

static void argonone_dbus_signal(GDBusProxy *proxy, gchar *sender_name, 
                                 gchar *signal_name, GVariant *parameters, gpointer user_data) {
  ArgonOnePlugin *aone = (ArgonOnePlugin *)user_data;
  gboolean fan_changed = FALSE;

  if (sender_name == NULL)  return;  /* Synthesized event from object manager */
  
  if (!g_strcmp0(signal_name, "NotifyValue")) {
    gchar *event_name;
    GVariant *value_var;
    g_variant_get(parameters, "(sv)", &event_name, &value_var);
    if (!g_strcmp0(event_name, NOTIFY_VALUE_FAN_SPEED)) {
      g_variant_get(value_var, "i", &(aone->fan_speed));
      fan_changed = TRUE;
    } else if (!g_strcmp0(event_name, NOTIFY_VALUE_FAN_CONTROL_ENABLED)) {
      g_variant_get(value_var, "b", &(aone->is_fan_control_enabled));
      fan_changed = TRUE;
    } else if (!g_strcmp0(event_name, NOTIFY_VALUE_TEMPERATURE)) {
      g_variant_get(value_var, "d", &(aone->temperature));
    }
    g_free(event_name);
  }

  /* Refresh UI view */
  argonone_update_view(aone, fan_changed, FALSE);
}

GVariant *argonone_dbus_method_call_sync(GDBusProxy *proxy, const gchar *method_name, GVariant *parameters) {
  GError *error = NULL;

  
  if (parameters != NULL && !g_variant_is_of_type(parameters, G_VARIANT_TYPE_TUPLE)) {
    GVariant **t = (GVariant *[]){ parameters };
    parameters = g_variant_new_tuple(t, 1);
  }

  GVariant *retval = g_dbus_proxy_call_sync(proxy, method_name, parameters, 0, -1, NULL, &error);
  if (error) {
    DEBUG("Failed to call %s method: %s", method_name, error->message);
    g_error_free(error);
  }
  return retval;
}


gboolean argonone_dbus_query_sync(GDBusProxy *proxy, const gchar *method_name, const gchar *return_format_string, gpointer value) {
  GVariant *retval;
  g_assert(value != NULL);
  if (!(retval = argonone_dbus_method_call_sync(proxy, method_name, NULL)))
    return FALSE;
  g_variant_get(retval, return_format_string, value);
  g_variant_unref(retval);
  return TRUE;
}

/***********************************************
 * Plugin popup menu                           */

/* TODO should we use async call for menu handlers, esp since there is no return value? */
static void _argonone_set_fan(ArgonOnePlugin *aone, gboolean enabled, gint32 fan_speed) {
  argonone_dbus_method_call_sync(aone->proxy, "SetFanControlEnabled", g_variant_new_boolean(enabled));
  if (fan_speed >= 0)  /* -1 denotes "keep current" */
    argonone_dbus_method_call_sync(aone->proxy, "SetFanSpeed", g_variant_new_int32(fan_speed));
  /* aone "model" will be updated via received signal, which acts as ACK */
}

static void argonone_resume_fan(GtkWidget *widget, ArgonOnePlugin *aone) {
  _argonone_set_fan(aone, TRUE, -1);
}

static void argonone_pause_off_fan(GtkWidget *widget, ArgonOnePlugin *aone) {
  _argonone_set_fan(aone, FALSE, 0);
}

static void argonone_pause_max_fan(GtkWidget *widget, ArgonOnePlugin *aone) {
  _argonone_set_fan(aone, FALSE, 100);
}

static void argonone_pause_current_fan(GtkWidget *widget, ArgonOnePlugin *aone) {
  _argonone_set_fan(aone, FALSE, -1);
}

static void argonone_build_popup_menu(ArgonOnePlugin* aone) {
  aone->popup_menu = gtk_menu_new();

  GtkWidget *resume_item = gtk_menu_item_new_with_label ("Resume");
  gtk_menu_shell_append(GTK_MENU_SHELL(aone->popup_menu), resume_item);
  g_signal_connect(resume_item, "activate", G_CALLBACK(argonone_resume_fan), aone);

  GtkWidget *pause_off_item = gtk_menu_item_new_with_label ("Hold stopped");
  gtk_menu_shell_append(GTK_MENU_SHELL(aone->popup_menu), pause_off_item);
  g_signal_connect(pause_off_item, "activate", G_CALLBACK(argonone_pause_off_fan), aone);

  GtkWidget *pause_max_item = gtk_menu_item_new_with_label ("Hold maximum");
  gtk_menu_shell_append(GTK_MENU_SHELL(aone->popup_menu), pause_max_item);
  g_signal_connect(pause_max_item, "activate", G_CALLBACK(argonone_pause_max_fan), aone);

  GtkWidget *pause_current_item = gtk_menu_item_new_with_label("Hold current");
  gtk_menu_shell_append(GTK_MENU_SHELL(aone->popup_menu), pause_current_item);
  g_signal_connect(pause_current_item, "activate", G_CALLBACK(argonone_pause_current_fan), aone);

  gtk_widget_show_all(aone->popup_menu);
}

static void argonone_popup_menu_set_position(GtkMenu *menu, gint *px, gint *py, gboolean *push_in, gpointer user_data) {
  ArgonOnePlugin *aone = (ArgonOnePlugin *)user_data;

  /* Determine the coordinates */
  lxpanel_plugin_popup_set_position_helper (aone->panel, aone->plugin, GTK_WIDGET(menu), px, py);
  *push_in = TRUE;
}

/***********************************************
 * Configuration settings                      */

static void argonone_update_from_settings(ArgonOnePlugin *aone) {
  int value;
  if (config_setting_lookup_int (aone->settings, "ShowLabel", &value))
    aone->show_label = (value == 1);
  if (config_setting_lookup_int (aone->settings, "IncludeTemperature", &value))
    aone->include_temperature = (value == 1);
}

/* Handler for system config changed message from panel */
static void argonone_configuration_changed(LXPanel *panel, GtkWidget *widget)
{
    ArgonOnePlugin *aone = lxpanel_plugin_get_data(widget);
    argonone_update_from_settings(aone);
    argonone_update_view(aone, FALSE, TRUE);
}

static gboolean argonone_apply_configuration(gpointer user_data)
{
    ArgonOnePlugin *aone = lxpanel_plugin_get_data((GtkWidget *)user_data);

    config_group_set_int(aone->settings, "ShowLabel", (int)aone->show_label);
    config_group_set_int(aone->settings, "IncludeTemperature", (int)aone->include_temperature);

    argonone_update_view(aone, FALSE, TRUE);

    return TRUE;
}

static GtkWidget *argonone_configure_dialog(LXPanel *panel, GtkWidget *widget)
{
    ArgonOnePlugin *aone = lxpanel_plugin_get_data(widget);

    /* No chance we will reject settings, so we use aone->{show_label,include_temperature} */
    return lxpanel_generic_config_dlg(
      "ArgonOne fan", panel,
      argonone_apply_configuration, widget,
      "Show label", &aone->show_label, CONF_TYPE_BOOL,
      "Include temperature", &aone->include_temperature, CONF_TYPE_BOOL,
      NULL
    );
}

/***********************************************
 * Plugin widget                               */

static gboolean argonone_button_press_event(GtkWidget *widget, GdkEventButton *event, LXPanel *panel) {
  ArgonOnePlugin *aone = lxpanel_plugin_get_data(widget);
  
  if (event->button == 1) {
    /* Left-click, toggle pause-off/resume fan control */
    if (aone->is_fan_control_enabled) {
      _argonone_set_fan(aone, FALSE, 0);
    } else {
      _argonone_set_fan(aone, TRUE, -1);
    }
  } else if (event->button == 3) {
    /* Right-click, show popup menu */
    if (aone->popup_menu == NULL) argonone_build_popup_menu(aone);
    gtk_menu_popup(GTK_MENU(aone->popup_menu), NULL, NULL, argonone_popup_menu_set_position, aone, event->button, event->time);
    return TRUE;
  }

  return FALSE;
}

#define STATUS_SIZE 32  /* Should never exceed 12 chars +1 '\0' -> 13 bytes */

/* Update all widgets, based on current plugin properties */
static void argonone_update_view (ArgonOnePlugin *aone, gboolean fan_changed, gboolean config_updated) {
  gchar status[STATUS_SIZE];
  gint speed_len;

  /* Update icon */
  if (fan_changed) {
    gchar *icon_name = NULL;
    if (!aone->is_fan_control_enabled) {
      icon_name = "argonone-fan-paused";
    } else if (aone->fan_speed == 0) {
      icon_name = "argonone-fan";
    } else if (aone->fan_speed <= 50) {
      icon_name = "argonone-fan-medium";
    } else {
      icon_name = "argonone-fan-high";
    }
    lxpanel_plugin_set_taskbar_icon(
        aone->panel, aone->tray_icon,
        icon_name);
  }

  if (config_updated)
    gtk_widget_set_visible(aone->tray_label, aone->show_label);

  if (config_updated || fan_changed || aone->include_temperature) {  /* Assume temperature always changes */
    /* Construct status string with fan speed and (optionally) temperature */
    if (aone->fan_speed < 0) {
      speed_len = g_snprintf(status, STATUS_SIZE, " -- ");
    } else {
      speed_len = g_snprintf(status, STATUS_SIZE, "%3d%%", aone->fan_speed);
    }
    if (aone->include_temperature) {
      g_snprintf(status + speed_len, STATUS_SIZE - speed_len, " / %4.1fC", aone->temperature);
    }

    /* Update label and/or tooltip */
    if (aone->show_label) {
      // gtk_label_set_width_chars(GTK_LABEL(aone->tray_label), aone->include_temperature ? 12 : 4);
      gtk_label_set_text(GTK_LABEL(aone->tray_label), status);
      if (config_updated) {
        gtk_widget_set_tooltip_text(aone->plugin, "ArgonOne fan");
      }
    } else {
      gtk_widget_set_tooltip_text(aone->plugin, status);
    }
  }
}

/* Plugin destructor */
static void argonone_destructor(gpointer user_data)
{
  ArgonOnePlugin *aone = (ArgonOnePlugin *)user_data;
  
  if (aone->popup_menu != NULL) gtk_widget_destroy(aone->popup_menu);
  
  g_object_unref(aone->proxy);

  /* TODO ? */

  g_free(aone);
}


/* Plugin constructor */
static GtkWidget *argonone_constructor(LXPanel *panel, config_setting_t *settings)
{
  /* Allocate and initialize plugin context */
  ArgonOnePlugin *aone;
  GtkWidget *hbox;
  GError *error;

  aone = g_new0(ArgonOnePlugin, 1);

  aone->show_label = TRUE;
  aone->include_temperature = FALSE;

  /* Initial values for startup view, should be updated ASAP via D-Bus */
  aone->is_fan_control_enabled = TRUE;
  aone->fan_speed = -1;

  aone->popup_menu = NULL;

  /* Allocate top level widget and set into Plugin widget pointer */
  aone->panel = panel;
  aone->plugin = gtk_button_new();
  gtk_button_set_relief(GTK_BUTTON(aone->plugin), GTK_RELIEF_NONE);
  g_signal_connect(aone->plugin, "button-press-event", G_CALLBACK(argonone_button_press_event), aone->panel);
  aone->settings = settings;
  lxpanel_plugin_set_data(aone->plugin, aone, argonone_destructor);
  gtk_widget_add_events(aone->plugin, GDK_BUTTON_PRESS_MASK);
  gtk_widget_set_tooltip_text(aone->plugin, "ArgonOne fan");

  /* Allocate children of top-level */
  hbox = gtk_hbox_new(FALSE, 2);
  aone->tray_icon = gtk_image_new();
  gtk_box_pack_start(GTK_BOX(hbox), aone->tray_icon, TRUE, TRUE, 0);
  aone->tray_label = gtk_label_new(NULL);
  gtk_box_pack_start(GTK_BOX(hbox), aone->tray_label, TRUE, TRUE, 0);
  gtk_container_add (GTK_CONTAINER (aone->plugin), hbox);

  /* Update "model" from config settings */
  argonone_update_from_settings(aone);

  /* Set up D-Bus connection, using object manager */
  error = NULL;
  aone->proxy = g_dbus_proxy_new_for_bus_sync(G_BUS_TYPE_SYSTEM, 0, NULL, "net.clusterhack.ArgonOne", "/net/clusterhack/ArgonOne", "net.clusterhack.ArgonOne", NULL, &error);
  if (error) {
    g_error("Failed to get dbus proxy: %s", error->message);
    g_error_free(error);
    /* gtk_exit(-1); */
  }
  g_signal_connect(aone->proxy, "g-signal", G_CALLBACK(argonone_dbus_signal),
                   aone);

  /* Retrieve current fan control and speed values; blocking is ok on startup */
  argonone_dbus_query_sync(aone->proxy, "GetFanSpeed", "(i)", &(aone->fan_speed));
  argonone_dbus_query_sync(aone->proxy, "GetFanControlEnabled", "(b)", &(aone->is_fan_control_enabled));
  argonone_dbus_query_sync(aone->proxy, "GetTemperature", "(d)", &(aone->temperature));

  /* Update UI view */

  /* Show widget and return */
  gtk_widget_show_all(aone->plugin);
  argonone_update_view(aone, TRUE, TRUE);  /* After _show_all(), since it updates label visibility */
  return aone->plugin;
}


FM_DEFINE_MODULE(lxpanel_gtk, argonone)

/* Plugin descriptor */
LXPanelPluginInit fm_module_init_lxpanel_gtk = {
  .name = "ArgonOne",
  .description = "ArgonOne case fan monitoring and control",
  .new_instance = argonone_constructor,
  .reconfigure = argonone_configuration_changed,
  .button_press_event = argonone_button_press_event,
  .config = argonone_configure_dialog
};
