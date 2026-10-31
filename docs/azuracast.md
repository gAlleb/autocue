# autocue with AzuraCast

[Back to the main README](../README.md#azuracast-integration)

Use one provider for ready AzuraCast cue points and the ordinary `cue_file`
fallback. Keep **AutoCue enabled** in the station UI, disable the built-in
crossfade, and apply one custom transition.

- [Upload the files](#upload-the-files)
- [Connect the provider](#connect-the-provider)
- [Replace the generated crossfade](#replace-the-generated-crossfade)
- [Live-aware add transition](#live-aware-add-transition)
- [Metadata and cache behavior](#metadata-and-cache-behavior)
- [Check the installation](#check-the-installation)

## Upload the files

Use these three files from the same maintained checkout:
[`cue_file`](../cue_file), [`autocue.cue_file.liq`](../autocue.cue_file.liq)
and [`autocue.azuracast.liq`](../autocue.azuracast.liq).
The two `.liq` files must stay together. The Python CLI needs Python 3,
FFmpeg/FFprobe and the dependencies described in the main README.

For a bind-mounted stations directory, run this **from your local autocue
checkout**, replacing the host, host directory and station short name:

```sh
AZ_HOST=user@your-radio-server
STATIONS_DIR=/home/user/azuracast/stations
STATION=your_station

ssh "$AZ_HOST" "mkdir -p '$STATIONS_DIR/$STATION/media/autocue'"
scp cue_file autocue.cue_file.liq autocue.azuracast.liq \
  "$AZ_HOST:$STATIONS_DIR/$STATION/media/autocue/"
ssh "$AZ_HOST" "chmod 755 '$STATIONS_DIR/$STATION/media/autocue/cue_file'"
```

`STATIONS_DIR` is the path **on the host**. In Liquidsoap, use the corresponding
path **inside the container**, normally
`/var/azuracast/stations/your_station/media/autocue/`.
For installations using a Docker named volume, copy the files into that volume
instead; do not assume it exists at `/home/user/azuracast/stations`.
The files must be readable, and `cue_file` executable, by the station process.

## Connect the provider

In the station's broadcasting settings, enable **AutoCue**. Disable its
crossfade option; the generated operator must also be removed as described
below.

Put this in the custom Liquidsoap field **before playlists**
(`custom_config_pre_playlists`), replacing `your_station`:

```liquidsoap
%include "/var/azuracast/stations/your_station/media/autocue/autocue.azuracast.liq"
settings.autocue.cue_file.path :=
  "/var/azuracast/stations/your_station/media/autocue/cue_file"
settings.autocue.amplify_behavior := "keep"
```

This field follows AzuraCast's common runtime and precedes source creation.
Do **not** use `custom_config_top`, which runs before the common runtime.
Replace any previous autocue include; the integration includes
`autocue.cue_file.liq` and selects the provider itself. AzuraCast already calls
`enable_autocue_metadata()`, so do not call it again. Put optional
`settings.autocue.cue_file.*` settings after the include.

Keep the default `liq_` prefix for existing tagged libraries. For a different
namespace, set `settings.autocue.cue_file.tag_prefix := "ac_"` (or anything you like -
`"bananas_"`) and use matching file tags. AzuraCast's own request fields keep their
fixed names.

Leave `settings.azuracast.compute_autocue` under the UI's control. It can be
`true`: our integration only calls AzuraCast's provider for ready values and
uses `cue_file` otherwise, so it does not invoke AzuraCast's internal analyzer.
Preserve your existing amplification chain; this integration does not add an
`amplify` operator. With the default `keep` policy, a request's `liq_amplify`
retains priority, including gain-only requests that use the fallback.

When replacing an old customized provider, remove any obsolete metadata map
that reads `liq_cross_start_next_override`: this provider does not emit it.
Old custom timing rules are not automatically carried into the new provider.

## Replace the generated crossfade

The UI switch alone does not necessarily remove this generated call:

```liquidsoap
radio = azuracast.apply_crossfade(radio)
```

Exclude it persistently before adding your transition. Editing the generated
`liquidsoap.liq` by hand is temporary: AzuraCast can regenerate it.

One option is [RM-FM's ls-config-replace plugin](https://github.com/RM-FM/ls-config-replace),
installed using [AzuraCast's plugin instructions](https://www.azuracast.com/docs/developers/plugins/).
Download the plugin to a persistent host directory, then merge this fragment
into your existing `docker-compose.override.yml` (replace the host path):

```yaml
services:
  web:
    environment:
      AZURACAST_PLUGIN_MODE: true
    volumes:
      - /absolute/path/ls-config-replace:/var/azuracast/www/plugins/ls-config-replace:ro
```

Preserve existing environment entries and volumes. A newly added mount needs
the service to be recreated through your normal AzuraCast deployment process;
an already mounted plugin does not need a new mount for each rule edit.
In its `liq/12_remove_crossfade/` directory, use these two files:

`find.liq`:

```liquidsoap
radio = azuracast.apply_crossfade(radio)
```

`replace.liq`:

```liquidsoap
# radio = azuracast.apply_crossfade(radio)
```

Review the plugin's bundled replacements before enabling it. Its upstream
replacement rules apply to every matching station configuration, so scope
your installation appropriately before removing crossfades on other stations.
After saving the station configuration, inspect the generated result: the
built-in call must be commented and there must be only one active `cross`.
Recheck the match after AzuraCast updates.

## Live-aware add transition

Put this in the custom field **before live broadcasting**
(`custom_config_pre_live`), after removing the built-in crossfade. If you
already have a custom `cross`, replace that block rather than adding a second.
The transition-to-live branch retains the existing sequential fades.

<!-- azuracast-crossfade-example -->
```liquidsoap
def live_aware_crossfade(old, new) =
    if azuracast.to_live() then
        sequence([
            fade.out(duration=2.5, old.source),
            fade.in(duration=0.1, new.source)
        ])
    else
%ifversion >= 2.5.0
        # cross supplies the outgoing buffer's next-entry delay in 2.5.
        # An explicit incoming delay keeps priority.
        fade_in_delay = new.metadata["liq_fade_in_delay"]
        fade_in_delay =
            if fade_in_delay == "" then
                old.metadata["liq_fade_out_start_next"]
            else
                fade_in_delay
            end
        fade_in_metadata =
            if fade_in_delay == "" then
                new.metadata
            else
                [
                    ...list.assoc.remove("liq_fade_in_delay", new.metadata),
                    ("liq_fade_in_delay", fade_in_delay)
                ]
            end
%else
        fade_in_metadata = new.metadata
%endif
        add(normalize=false, [
            fade.in(
                type="lin",
                initial_metadata=fade_in_metadata,
                duration=settings.autocue.cue_file.fade_in(),
                new.source
            ),
            fade.out(
                type="lin",
                initial_metadata=old.metadata,
                duration=settings.autocue.cue_file.fade_out(),
                old.source
            )
        ])
    end
end

radio = cross(
    duration=settings.autocue.cue_file.fade_out(),
    live_aware_crossfade,
    radio
)
```

`%ifversion` is evaluated by Liquidsoap: 2.4.x keeps the original incoming
metadata; 2.5 and later use the delay transfer. This coordinates when the
incoming fade begins; it does not recalculate autocue's cue points.
Explicit incoming `liq_fade_in_delay` wins, and absent delay leaves metadata
unchanged. This example uses linear fades for ordinary tracks.
Use a 2.5 build with the upstream fixes listed in the main README.
Full DJ connect/disconnect behavior still needs verification in your station.

## Metadata and cache behavior

- **Ready data:** valid request annotations with `azuracast_autocue="true"`
  are passed through AzuraCast's provider to Liquidsoap's metadata adapter.
  They can originate from a media card or computed cache. This branch does
  not call `cue_file` or save the ready bundle again.
- **Fallback:** without valid ready data, ordinary `cue_file` reads complete
  tags or analyzes audio. Invalid ready bundles fall back as a whole,
  preserving gain and discarding their invalid cue fields.
- **Saving:** a successful fallback with `azuracast_cache_key` saves its final
  cue/fade points, `start_next` and gain asynchronously through `savecache`.
  Later requests can use these values without running the CLI. Missing keys,
  skipped processing and failures do not create entries; API failures leave
  playback metadata available.
- **Gain:** cache gain is stored in dB. Linear gain is converted first; a
  linear mute (`"0"`) skips caching because it has no finite dB value. The
  `ignore` policy omits computed gain so it cannot return as an annotation.
- **Duration:** full-file `duration` stays unchanged. Informational
  `<prefix>cue_duration` is derived from the final `cue_out - cue_in` on both
  paths, including cached and manual values. It includes the faded audio;
  it changes if the provider moves `cue_out`. Other analysis-only metadata
  is not stored in the cache and need not be identical on both paths.

**Entering only `cue_in` in a media card still selects AzuraCast's complete
bundle.** It fills the missing `cue_out` from file length and missing fades
from station defaults (zero with crossfade disabled). `cue_file` does not
fill those missing fields. `start_next` may be absent.

Card fields persist across station restarts. Computed-cache storage, expiry
and its priority relative to card fields depend on the AzuraCast version.
For example, tested rolling build `53e517d` uses a 24-hour cache and can let
cached fields override card values; newer code has different behavior.
Keep AutoCue enabled for this installation procedure: older versions may
send cache keys even with it disabled, while newer versions gate them on it.
After changing analysis or transition settings, invalidate the affected
computed cache to force new fallback results. Restarting Liquidsoap alone
does not clear it, and clearing cache does not remove card values.

## Check the installation

Save the configuration and restart the station when ready. Look for:

```text
No ready AzuraCast cue/fade data ...; using cue_file.
Using existing tags; audio analysis skipped ...
Full audio analysis performed ...
AzuraCast accepted cue_file cache result ...
Using ready AzuraCast cue/fade annotations ...
```

The two analysis messages describe alternative fallback paths. An API
acceptance message alone is not proof of a later cache hit: replay the same
file, check that it uses ready data without a CLI call, and distinguish that
from already populated media-card fields. Do not manually fill cue fields
when testing the fallback path.

The provider has local integration coverage on Liquidsoap 2.4.5 and 2.5,
including custom prefixes, cache round trips, gain policies and API failures.
Real playback and cache storage were also checked on AzuraCast `53e517d`
with Liquidsoap 2.4.6. This is not a full test of every AzuraCast release or
live-DJ workflow.
