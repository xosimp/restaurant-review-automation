The Ember Core — Cavnar AI's intelligence, drawn (10/8/26)

The same molten core the website, the dashboard and the iOS app draw live
(public/static/ember-core.js, static/ember-core.js, ios/.../EmberCore.metal).
Use these where the AI itself is the subject: decks, social posts, video
openers, App Store and press imagery. Never as a loading spinner — the dotted
orb is the working state.

Stills (rendered by the real shader, not a mock-up)
  ember-core-2048.png / -1024.png / -512.png   transparent background; the
      core sits in the middle with room for its glow and orbiting embers
  ember-core-on-dark-1920x1080.png             on the brand ground #0b0907
  ember-core-on-dark-1080x1080.png

Video (12 s, 30 fps, H.264, seamless loop — the last 1.5 s fades into the
first, so it can repeat forever)
  ember-core-loop-1080x1080.mp4                square: social, decks
  ember-core-loop-1920x1080.mp4                landscape: video, web headers

Re-rendering (after the material changes)
  1. cp public/static/ember-core.js brand/ember-core/render/
  2. python3 brand/ember-core/render/server.py      (serves :8799, saves frames)
  3. open http://127.0.0.1:8799/render.html in Chrome and run, in the console:
       await renderJob({dir:'loop-sq', css:238, frames:525, sub:2, skip:120})
       await renderJob({dir:'still-2048', css:450, frames:300, sub:2, skip:299})
     (the page drives the engine's clock itself: every frame is exact)
  4. swiftc -O brand/ember-core/render/encode.swift -o /tmp/encode
     /tmp/encode render/frames/loop-sq out-1080x1080.mp4 1080 1080 360 45
     /tmp/encode render/frames/loop-sq out-1920x1080.mp4 1920 1080 360 45
     (composites onto #0b0907, adds a ±1 grain so the dark glow doesn't band,
      and cross-fades the tail into the head)
