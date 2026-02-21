#!/usr/bin/liquidsoap

# Family Radio Station - Liquidsoap script

settings.init.allow_root.set(true)
settings.server.telnet.set(true)
settings.server.telnet.bind_addr.set("0.0.0.0")
settings.server.timeout.set(30.)

icecast_host = "icecast"
icecast_port = 8000
icecast_password = environment.get("ICECAST_SOURCE_PASSWORD")
api_base = "http://api:8000"
server_hostname = environment.get("SERVER_HOSTNAME")

def get_next_track() =
  log("Requesting next track from API...")
  result = try
    response = http.get("#{api_base}/internal/next-track", timeout=10.)
    path = string.trim(response)
    log("Next track: #{path}")
    if path == "" or string.length(path) == 0 then
      log("Empty path returned, using silence")
      null()
    else
      request.create(path)
    end
  catch err do
    log("Error fetching next track: #{err}")
    null()
  end
  result
end

dynamic = request.dynamic(
  id="dynamic",
  get_next_track,
  timeout=20.
)

def on_track(m) =
  track_id = m["comment"]
  if not (track_id == "") then
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

tracked = source.on_metadata(dynamic, on_track)

silence = blank(id="silence", duration=10.0)

radio = fallback(id="radio", track_sensitive=true, [tracked, silence])

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
  url="http://#{server_hostname}",
  public=false,
  fallible=true,
  radio
)
