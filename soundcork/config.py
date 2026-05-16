from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Create the settings.

    Don't populate here. The variables are only declared to make life
    easier for IDE autocomplete. Populate in .env.shared -- or, if
    committing to source control, .env.private (which is in the
    .gitignore).

    Source for each of these strings:

    Unless otherwise specified all files are on you speaker in:
    /var/volatile/lib/Bose/PersistenceDataRoot/BoseApp-Persistence/1

    - device_id: Recents.xml

    """

    # base url for the soundcork server. this should be reachable by the speakers
    base_url: str = ""

    # local directory where soundcork stores its data
    data_dir: str = ""

    # Spotify OAuth (optional — leave empty to disable)
    spotify_client_id: str = ""
    spotify_client_secret: str = ""
    spotify_redirect_uri: str = ""

    # (optional) local directory for soundcork to store detailed logs of 404 errors
    #  used for development/debugging
    unhandled_log_dir: str = ""

    # Enable the web GUI (miniapp + admin UI). Set to false for API-only mode.
    enable_gui: bool = True

    # IP address of the Bose SoundTouch speaker (used by start.sh)
    speaker_ip: str = ""

    # Music Assistant integration (optional)
    # URL of the Music Assistant web UI, e.g. http://192.168.1.100:8095
    music_assistant_url: str = ""
    # Direct HTTP audio stream exposed by Music Assistant (e.g. via Snapcast / virtual player)
    # e.g. http://192.168.1.100:1704/stream
    music_assistant_stream_url: str = ""

    model_config = SettingsConfigDict(
        # `.env.private` takes priority over `.env.shared`
        env_file=(".env.shared", ".env.private")
    )
