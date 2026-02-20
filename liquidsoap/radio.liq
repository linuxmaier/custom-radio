#!/usr/bin/liquidsoap

# Family Radio Station - Liquidsoap script

settings.server.timeout.set(30.)
settings.request.metadata_decoders.set(["FFMPEG"])

icecast_host = "icecast"
icecast_port = 8000
icecast_password = environment.get("ICECAST_SOURCE_PASSWORD")
api_base = "http://api:8000"

def get_next_track() =
  log("Requesting next track from API...")
  result = try
    response = http.get("#{api_base}/internal/next-track", timeout=10.)
    path = string.trim(response.contents)
    log("Next track: #{path}")
    if path == "" or string.length(path) == 0 then
      log("Empty path returned, using silence")
      []
    else
      [request.create(path)]
    end
  catch err do
    log("Error fetching next track: #{err}")
    []
  end
  result
end

dynamic = request.dynamic.list(
  id="dynamic",
  get_next_track,
  conservative=true,
  timeout=20.
)

def on_track(m) =
  track_id = m["comment"]
  if track_id != "" then
    log("Track started: #{track_id}")
    try
      ignore(http.post(
        "#{api_base}/internal/track-started/#{track_id}",
        data="",
        timeout=5.
      ))
    catch err do
      log("Error posting track-started: #{err}")
    end
  end
end

tracked = source.on_metadata(on_track, dynamic)

silence = blank(id="silence", duration=10.0)

radio = fallback(id="radio", track_sensitive=true, [tracked, silence])

radio = crossfade(
  id="crossfaded",
  duration=3.0,
  fade_out=3.0,
  fade_in=3.0,
  radio
)

output.icecast(
  %mp3(bitrate=128, samplerate=44100, stereo=true),
  id="icecast_out",
  host=icecast_host,
  port=icecast_port,
  password=icecast_password,
  mount="/radio",
  name="Family Radio",
  description="Our family radio station",
  genre="Various",
  url="http://#{environment.get("SERVER_HOSTNAME")}",
  public=false,
  fallible=true,
  radio
)
