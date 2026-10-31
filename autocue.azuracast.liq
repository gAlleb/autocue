# autocue: optional AzuraCast integration
# 2026-10-31 Stefan (gAlleb) - Prefer ready AzuraCast annotations; otherwise
# use the ordinary cue_file provider and save its result to AzuraCast's cache.
# Keep cue_duration consistent with final cue points for ready data too.
# Include AFTER AzuraCast's common runtime, INSTEAD OF autocue.cue_file.liq.
%include "autocue.cue_file.liq"

if not list.assoc.mem("azuracast", settings.autocue.implementations()) then
  error.raise(error.not_found,
    "autocue.azuracast.liq must be included after AzuraCast's common runtime.")
end

azuracast_cue_file_native =
  list.assoc("azuracast", settings.autocue.implementations())

%ifdef azuracast.api_call
azuracast_cue_file_api = azuracast.api_call
%else
# Keep the missing-runtime error above readable during standalone validation.
def azuracast_cue_file_api(~timeout, action, payload) =
  ignore(timeout)
  ignore(action)
  ignore(payload)
  error.raise(error.not_found, "AzuraCast API helper is unavailable")
end
%endif

# These are AzuraCast's request fields, not configurable file tag names.
azuracast_cue_file_fields = [
  "autocue_cue_in", "autocue_cue_out", "autocue_fade_in",
  "autocue_fade_out", "autocue_start_next"
]

def azuracast_cue_file_valid(metadata) =
  def number(key) =
    value = float_of_string(list.assoc(key, metadata))
    # Reject NaN and infinities as well as negative times.
    if value < 0. or value - value != 0. then
      error.raise(error.invalid, "Invalid AzuraCast time: " ^ key)
    end
    value
  end
  try
    cue_in = number("autocue_cue_in")
    cue_out = number("autocue_cue_out")
    ignore(number("autocue_fade_in"))
    ignore(number("autocue_fade_out"))
    valid_start =
      if list.assoc.mem("autocue_start_next", metadata) then
        start_next = number("autocue_start_next")
        cue_in <= start_next and start_next <= cue_out
      else true end
    cue_in < cue_out and valid_start
  catch _ do false end
end

def azuracast_cue_file_save(~cache_key, result) =
  # Save only the provider contract, not informational file metadata. AzuraCast
  # stores amplify in dB, while Liquidsoap also accepts linear gain strings.
  # A zero linear gain (mute) cannot be represented as a finite cached dB value.
  try
    def cached_gain =
      # Cached gain returns as a request annotation. Do not turn an ignored
      # provider gain into an authoritative annotation on the next play.
      if settings.autocue.amplify_behavior() == "ignore" then {} else
        gain = metadata.parse.amplify(result.amplify)
        if gain - gain != 0. then
          error.raise(error.invalid, "Gain cannot be represented as finite dB")
        end
        {amplify=gain}
      end
    end
    payload = json.stringify({
      cache_key=cache_key,
      data={
        cue_in=result.cue_in, cue_out=result.cue_out,
        fade_in=result.fade_in, fade_out=result.fade_out,
        start_next=result.start_next, ...cached_gain
      }
    })
    def save() =
      try
        response = azuracast_cue_file_api(timeout=5.0, "savecache", payload)
        saved =
          if null.defined(response) then
            let json.parse (ok : bool) = null.get(response)
            ok
          else false end
        if saved then
          log(level=3, label="autocue.azuracast", "AzuraCast accepted cue_file cache result for #{cache_key}.")
        else
          log(level=2, label="autocue.azuracast", "AzuraCast did not accept cue_file cache result for #{cache_key}; playback continues.")
        end
      catch err do
        log(level=2, label="autocue.azuracast", "Could not save cue_file cache result for #{cache_key}: #{err}; playback continues.")
      end
    end
    thread.run(fast=false, save)
  catch err do
    log(level=2, label="autocue.azuracast", "Skipping cue_file cache write for #{cache_key}: #{err}; playback continues.")
  end
end

def azuracast_cue_file(~request_metadata, ~file_metadata, filename) =
  label = "autocue.azuracast"
  ready = list.assoc(default="false", "azuracast_autocue", request_metadata) == "true"
  # An invalid AzuraCast bundle must not become custom-prefix overrides in
  # cue_file (in particular when tag_prefix is "autocue_"). Preserve gain.
  fallback_metadata =
    if ready then
      list.filter(fun (entry) -> not list.mem(fst(entry), azuracast_cue_file_fields), request_metadata)
    else request_metadata end

  result =
    if ready and azuracast_cue_file_valid(request_metadata) then
      try
        azuracast_cue_file_native(request_metadata=request_metadata,
          file_metadata=file_metadata, filename)
      catch err do
        log(level=2, label=label, "AzuraCast provider failed: #{err}")
        null
      end
    else null end

  if null.defined(result) then
    log(level=3, label=label, "Using ready AzuraCast cue/fade annotations for #{filename}.")
    ready_result = null.get(result)
    # Match cue_file's informational duration after its cue-out adjustments.
    # Derive it from final points, without changing full-file duration or
    # storing a redundant field in AzuraCast's cache.
    duration_key = string.case(settings.autocue.cue_file.tag_prefix()) ^ "cue_duration"
    extra_metadata = ready_result.extra_metadata ?? []
    { ...ready_result,
      extra_metadata = [
        (duration_key, string.float(decimal_places=2,
          ready_result.cue_out - ready_result.cue_in)),
        ...list.assoc.remove(duration_key, extra_metadata)
      ]
    }
  else
    if ready then
      log(level=2, label=label,
        "Invalid or unavailable AzuraCast cue/fade data for #{filename}; falling back to cue_file.")
    else
      log(level=3, label=label,
        "No ready AzuraCast cue/fade data for #{filename}; using cue_file.")
    end
    fallback = cue_file(request_metadata=fallback_metadata, file_metadata=file_metadata, filename)
    cache_key = list.assoc(default="", "azuracast_cache_key", request_metadata)
    if cache_key != "" and null.defined(fallback) then
      azuracast_cue_file_save(cache_key=cache_key, null.get(fallback))
    end
    fallback
  end
end

# Keep the cue_file identity: its custom-prefix annotation gain resolver
# intentionally runs only when this provider is selected. Replace only its
# registration, retaining the ordinary cue_file function for the fallback.
settings.autocue.implementations :=
  list.assoc.remove("cue_file", settings.autocue.implementations())
autocue.register(name="cue_file", azuracast_cue_file)
settings.autocue.preferred := "cue_file"
