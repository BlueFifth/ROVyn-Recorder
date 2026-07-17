# ROVyn-Recorder

Taken from the handover docs:

 The DWE StellarHD stereo cameras are currently both recording and streaming with a custom extension detailed below.
 This was necessary as the default formats recorded by the cameras (MJPEG and YUY2) are not supported by Dashcam or Cockpit, meaning neither streaming nor recording is supported by the out-the-box infrastructure.

Also worth noting is that the DWE cameras produce an extremely high amount of data, which the poor SD card absolutely cannot handle. As such, a portable SSD is plugged into the RPi's USB3 port to leverage the USB3 driver's speed. It's likely still not enough at the higher resolutions, especially as the USB speeds are split between all USB devices, not per port, but its a great step up.
##### A summary of the recording extension
The extension was partially vibe-coded, especially with relation to UI bits. I apologise if this results in massive technical debt, but the core functionality should only be a few lines of gstreamer commands. Due to time constraints it was intended to be a proof-of-concept, showing that the cameras are working, and how one would stream and record from them (simultaneously). This is all handled using gstreamer (as is all of the other camera stuff onboard, including the MAVLink camera manager).

Currently the extension offers dual-camera recording of YUY2 video, attempting all of the resolutions supported by the camera. The extension also encodes a lower (varying from 1/2 to 5 based on the drop-down menu) fps stream for topside, allowing for positioning around objects of interest while in the field using Cockpit. The stream is low fps as the RPi must encode every frame, which chews up a lot of processing power.

The extension includes time-stamping of the data via csv, however the validity of it is unknown due to the processor strain around getting anything from the camera. This wasn't bothered with as the more significant factor is that the hardware synchronisation allowed for by the cameras are not plugged in, and require a cable change from 4 wires to 6 wires to support this. This should be coordinated to be done by Chris at ROV Africa, our BlueRobotics supplier.

If work should be done without the sync cables, it was recommended to read a paper by Pretorius and Boje about frame sync estimation. I suspect it's this paper [here][https://www.sciencedirect.com/science/article/pii/S2405896317324357], but we can also just talk to Arnold. 

A brief experiment was done on the final version of the extension, running the recorder and streamer for a few minutes on each resolution to get a baseline impression of functionality.
The results are as follows:

320x240 @15fps test:
- Got extra frames (ffmpeg found more frames than framerate x duration)
- Streaming worked fine during the recording (@1 fps)
320x240 @30fps test:
- Streaming @1fps still fine
- Lost ~100 frames for cam1 over a 187s window
- Gained ~30 frames for cam0 over same window?
640x480@5fps test:
- Stream @1fps fine
- Cam0 fine - no frames lost on a 5 min window
- Cam1 lost ~95 frames over a 5 min window
1280x720@5fps test:
- Stream only working on one cam even without recording
- Didn't want to stop recording - took a while
- Cam0 recorded well for the ~2 mins tested
- Cam 1 corrupted/not happy
1600x1200 test:
-  Crashed BlueOS after 1 min of recording
- Only one camera (cam0) was streaming

During any extension use, packets seem unreliable, and also seem to strain the main (moncular) cameras stream.
This experiment was half-baked but should serve as an estimate of what the Pi 4 is capable of.
It's hoped that the bulk of the problems experienced were either compute or data bandwidth limitations, which will be resolved by the upgrade to a RPi 5.


>[!Note]
>While baseline functionality is still being developed on the stereo cams, its important that they're calibrated to be able to measure things underwater. Please refer to Adrienne Winter's MSc for guidance on underwater camera calibration.
