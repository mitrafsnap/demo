#!/bin/sh
# Turn tools/record.py output into the README's MP4 and GIF.
#
#   tools/encode.sh <record dir> <mp4 start s> <speed> <gif start s> <gif length s>
#
# Each MP4 opens with the two intro cards (7 s each), then plays the recording
# from <mp4 start> at <speed>x with a speed badge. demo-full.mp4 keeps the
# recording's resolution at high quality; demo.mp4 is 1280 px wide for the
# repository. The GIF is a real-time excerpt of the top GIF_HEIGHT pixels
# (default 1220: acts, diagram and lanes). Uses ffmpeg from a container.
set -eu
dir=$(cd "$1" && pwd); start=$2; speed=$3; gstart=$4; glen=$5
here=$(cd "$(dirname "$0")" && pwd)
cp "$here/../dashboard/fonts/cmunss.otf" "$dir/font.otf"
ff() { docker run --rm -v "$dir:/c" -w /c linuxserver/ffmpeg:version-7.1-cli -loglevel error -y "$@"; }
size=$(docker run --rm --entrypoint ffprobe -v "$dir:/c" linuxserver/ffmpeg:version-7.1-cli -v error \
  -select_streams v -show_entries stream=width,height -of csv=p=0:s=x /c/intro-1.png)
enc="-c:v libx264 -preset slower -tune animation -pix_fmt yuv420p -r 25"

# constant 25 fps timeline from the variable-rate screenshots
ff -f concat -safe 0 -i frames.txt -fps_mode cfr -r 25 -c:v libx264 -preset veryfast -crf 12 \
   -pix_fmt yuv420p timeline.mp4
badge="drawtext=fontfile=font.otf:text='${speed}x speed':fontsize=22:fontcolor=0xe0a84a:x=w-tw-28:y=h-th-20:box=1:boxcolor=0x0f1115@0.85:boxborderw=8"
# <name> <scale filter> <crf>
render() {
  for n in 1 2; do
    ff -loop 1 -t 7 -i "intro-$n.png" -vf "fade=in:0:12,$2" $enc -crf "$3" "intro-$n-$1.mp4"
  done
  ff -ss "$start" -i timeline.mp4 -an -vf "setpts=PTS/$speed,$badge,$2" $enc -crf "$3" "main-$1.mp4"
  printf "file 'intro-1-%s.mp4'\nfile 'intro-2-%s.mp4'\nfile 'main-%s.mp4'\n" "$1" "$1" "$1" > "$dir/parts-$1.txt"
  ff -f concat -safe 0 -i "parts-$1.txt" -c copy -movflags +faststart "$1.mp4"
}
render demo-full "scale=iw:ih" 18
render demo "scale=1280:-2:flags=lanczos" 23
ff -ss "$gstart" -t "$glen" -i timeline.mp4 \
   -vf "crop=iw:min(ih\,${GIF_HEIGHT:-1220}):0:0,fps=12,scale=1440:-1:flags=lanczos,split[a][b];[a]palettegen=max_colors=256:stats_mode=full[p];[b][p]paletteuse=dither=sierra2_4a" \
   demo-highlight.gif
# the same excerpt as a small looping video, for web pages
ff -ss "$gstart" -t "$glen" -i timeline.mp4 -an \
   -vf "crop=iw:min(ih\,${GIF_HEIGHT:-1220}):0:0,scale=1440:-2:flags=lanczos" \
   -c:v libx264 -preset slower -tune animation -crf 23 -pix_fmt yuv420p -movflags +faststart \
   demo-highlight.mp4
echo "frame size $size"
ls -la "$dir/demo-full.mp4" "$dir/demo.mp4" "$dir/demo-highlight.gif" "$dir/demo-highlight.mp4"
