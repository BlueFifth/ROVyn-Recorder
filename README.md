# QuickStart-Python-Extension

A quick-start repository for building and uploading a Python-focused BlueOS Extension altered and debugged for easier use.



## Intent

This is intended to showcase:
1. How to make a basic Extension with a simple web interface, using Python and some HTML
2. The difference between code running on the frontend vs the backend
    - Backend code has access to vehicle hardware and other service APIs, as well as the filesystem (for things like persistent logging)
    - Frontend code is in charge of the display, and runs in the browser interface (instead of on the vehicle's onboard computer)

## Usage

Forking the repository will try to automatically package and upload your Extension variant to a Docker registry (Docker Hub), using the built in GitHub Action.
This process makes use of some [GitHub Variables](https://github.com/BlueOS-community/Deploy-BlueOS-Extension#input-variables) that you can configure for your fork.

The github acttion has been supplimented with a local makefile. With the makefile you can do the same things as   the action, but with far more speed and convinience, as caching works better, and you can just use your local (terminal) docker login instead of using docker token passwords. Scan through the makefile to see the exectution options

To install this extension, add it as a custom extension in the BlueOS extension manager, copying the permissions from the dockerfile (lines 14-29), removing line seperators. It'll need to be accessable in a public Dockerhub repo. If ```make deploy``` runs correctly with your config you should be sorted.

Note that you'll need to use your own port. This is reflected in lines 7, 22, and 56 of the DOCKERFILE. If you would like to make your own extension, pick a new port and change these lines to reflect that.

