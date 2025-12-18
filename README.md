# TC2 Launcher

Manages downloading and launching TC2.

## Installation

### Windows

You need the WebView2 Runtime installed. If you have Windows 11, or a newer version of Windows 10,
you will already have it by default. If you don't have it, you can install it [here](https://go.microsoft.com/fwlink/p/?LinkId=2124703).

### Linux

You must have GTK with Python extensions installed in order to use pywebview.

#### GTK

```sh
sudo apt install python3-gi python3-gi-cairo gir1.2-gtk-3.0 gir1.2-webkit2-4.1
```

For other distributions, consult the [PyGObject documentation](https://pygobject.gnome.org/getting_started.html).

#### Qt

There is also a `TC2Launcher-linux-qt` available for KDE/Qt users. You may need to install these Qt extensions:

```sh
sudo apt install python3-pyqt5 python3-pyqt5.qtwebengine python3-pyqt5.qtwebchannel libqt5webkit5-dev
```
