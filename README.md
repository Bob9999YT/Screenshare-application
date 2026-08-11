> [!CAUTION]
> The one official application to use is this github repo (make sure its made by "Bob9999YT"). Eventually there will be a website to host the download to the same application

> [!IMPORTANT]
> This only works on windows as on now, however ports to macOS, linux and possibly even a android phone port are planned.

## Info

This is a application to give the URL needed to showcase your PC's screenshare in the roblox game "Screenshare".
It can run up to a resolution of 1920x1080 (HD).

## Frequently asked questions

**Q: is this malware**
**A: No, the source can be easily viewed and will never have anything of the sort. Just make sure you got it from this github repo by the user "Bob9999YT"**

**Q: is this vibe coded?**
**A: Admittedly yeah this was generated using AI. I dont really know how to code in python so this application itself is AI. However most of the roblox game is coded by myself (I only know how to script in luau)**

## Requirements

You need python 3.7+ to run this (but id recommend the latest version) 
You can install that [here](https://www.python.org/downloads/ )

You also need clouflared too (It tries to download this in the application itself however this will likely fail)
You can install that [here]([https://www.python.org/downloads/](https://github.com/cloudflare/cloudflared/releases/tag/2026.7.3) ) and this should be named as "
cloudflared-windows-amd64.exe"

## Installation
Download the [latest release](https://github.com/Bob9999YT/Screenshare-application/releases/tag/v1.00) and run the .exe file.

You can also manually install this if you would wish pressing the green "code" button and installing as a zip and running the following commands in your command prompt:
```
> cd "C:\PATH\TO\YOUR\EXTRACTED\ZIP"
> python -m PyInstaller --onefile --windowed --name ScreenShareApp app.py

:: If you don't have pyinstaller installed
> py -m pip install mss Pillow flask certifi pyinstaller
```
