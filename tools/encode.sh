#!/bin/sh
# Turn tools/record.py output into the README's MP4 and GIF.
#
#   tools/encode.sh <record dir> <mp4 start s> <speed> <gif start s> <gif length s>
#
# The MP4 starts at <mp4 start> of the recording and plays at <speed>x; the GIF
# is a real-time excerpt. Uses ffmpeg from a container.
set -eu
dir=$(cd "$1" && pwd); start=$2; speed=$3; gstart=$4; glen=$5
ff() { docker run --rm -v "$dir:/c" -w /c linuxserver/ffmpeg:version-7.1-cli -loglevel error -y "$@"; }

# constant 25 fps timeline from the variable-rate screenshots
ff -f concat -safe 0 -i frames.txt -fps_mode cfr -r 25 -c:v libx264 -preset veryfast -crf 12 \
   -pix_fmt yuv420p timeline.mp4
ff -ss "$start" -i timeline.mp4 -vf "setpts=PTS/$speed" -an -c:v libx264 -preset slower -tune animation -crf 22 \
   -pix_fmt yuv420p -movflags +faststart demo.mp4
ff -ss "$gstart" -t "$glen" -i timeline.mp4 \
   -vf "fps=12,scale=1440:-1:flags=lanczos,split[a][b];[a]palettegen=max_colors=256:stats_mode=full[p];[b][p]paletteuse=dither=sierra2_4a" \
   demo-highlight.gif
ls -la "$dir/demo.mp4" "$dir/demo-highlight.gif"
