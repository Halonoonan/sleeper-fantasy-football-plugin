# Sleeper Fantasy Football

Displays Sleeper fantasy football matchups and standings on LEDMatrix.

## Configuration

Use `league_id` for the most reliable setup. You can find it in Sleeper's league URL. If `league_id` is blank, the plugin can try to discover a current NFL league from `username`.

Optional team focus:

- Set `roster_id` if you know your Sleeper roster ID.
- Or set `username` / `favorite_team_name` and the plugin will try to focus on your matchup.

No Sleeper API token is required. The public Sleeper API is read-only.
