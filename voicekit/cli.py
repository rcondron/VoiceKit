"""VoiceKit CLI interface.

Provides the ``voicekit`` command-line tool for starting the daemon,
listing audio devices, generating configuration, and testing platforms.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from voicekit import __version__

app = typer.Typer(
    name="voicekit",
    help="VoiceKit — Universal AI Voice Bridge. Connect AI voice models to any call platform.",
    no_args_is_help=True,
)
console = Console()

DEFAULT_CONFIG_PATH = Path("config.yaml")


@app.command()
def start(
    config: Path = typer.Option(
        DEFAULT_CONFIG_PATH,
        "--config",
        "-c",
        help="Path to configuration file.",
    ),
) -> None:
    """Start the VoiceKit daemon."""
    from voicekit.config import load_config
    from voicekit.daemon import run_daemon

    if not config.exists():
        console.print(
            f"[red]Config file not found:[/red] {config}\n"
            "Run [bold]voicekit init[/bold] to generate an example config."
        )
        raise typer.Exit(1)

    try:
        cfg = load_config(config)
    except Exception as exc:
        console.print(f"[red]Invalid configuration:[/red] {exc}")
        raise typer.Exit(1)

    console.print(f"[bold green]VoiceKit v{__version__}[/bold green]")
    console.print(f"Config: {config}")
    console.print(f"Provider: {cfg.provider.type}")

    enabled = []
    if cfg.platforms.virtual_audio.enabled:
        enabled.append("virtual_audio")
    if cfg.platforms.telegram.enabled:
        enabled.append("telegram")
    if cfg.platforms.discord.enabled:
        enabled.append("discord")
    if cfg.platforms.zoom.enabled:
        enabled.append("zoom")
    if cfg.platforms.whatsapp.enabled:
        enabled.append("whatsapp")
    if cfg.platforms.signal.enabled:
        enabled.append("signal")
    if cfg.platforms.slack.enabled:
        enabled.append("slack")
    if cfg.platforms.sip.enabled:
        enabled.append("sip")

    console.print(f"Platforms: {', '.join(enabled) if enabled else '[yellow]none enabled[/yellow]'}")
    console.print("")

    try:
        asyncio.run(run_daemon(cfg))
    except KeyboardInterrupt:
        console.print("\n[yellow]Shutdown complete.[/yellow]")


@app.command()
def devices() -> None:
    """List available audio devices."""
    try:
        from voicekit.platforms.virtual_audio import VirtualAudioPlatform

        device_list = VirtualAudioPlatform.list_devices()
    except Exception as exc:
        console.print(f"[red]Failed to list devices:[/red] {exc}")
        console.print("Make sure [bold]sounddevice[/bold] is installed.")
        raise typer.Exit(1)

    if not device_list:
        console.print("[yellow]No audio devices found.[/yellow]")
        return

    table = Table(title="Audio Devices")
    table.add_column("Index", style="cyan", justify="right")
    table.add_column("Name", style="white")
    table.add_column("Inputs", style="green", justify="right")
    table.add_column("Outputs", style="blue", justify="right")
    table.add_column("Sample Rate", justify="right")

    for dev in device_list:
        table.add_row(
            str(dev["index"]),
            dev["name"],
            str(dev["max_input_channels"]),
            str(dev["max_output_channels"]),
            str(int(dev["default_samplerate"])),
        )

    console.print(table)


@app.command()
def init(
    output: Path = typer.Option(
        DEFAULT_CONFIG_PATH,
        "--output",
        "-o",
        help="Output path for the config file.",
    ),
    force: bool = typer.Option(
        False,
        "--force",
        "-f",
        help="Overwrite existing config file.",
    ),
) -> None:
    """Generate an example configuration file."""
    if output.exists() and not force:
        console.print(
            f"[yellow]Config file already exists:[/yellow] {output}\n"
            "Use [bold]--force[/bold] to overwrite."
        )
        raise typer.Exit(1)

    example_config = """\
# VoiceKit Configuration
# See https://github.com/voicekit/voicekit for documentation.

daemon:
  log_level: info
  # log_file: voicekit.log

provider:
  type: openai_realtime
  api_key: ${OPENAI_API_KEY}
  model: gpt-4o-realtime-preview
  voice: alloy
  instructions: |
    You are a helpful voice assistant. Be concise and natural.
  turn_detection:
    threshold: 0.5
    silence_duration_ms: 500
    prefix_padding_ms: 300

platforms:
  virtual_audio:
    enabled: true
    # Use 'voicekit devices' to find device names
    input_device: ""
    output_device: ""
    sample_rate: 24000
    channels: 1
    chunk_size: 4800

  telegram:
    enabled: false
    api_id: ${TELEGRAM_API_ID}
    api_hash: ${TELEGRAM_API_HASH}
    phone_number: ""
    session_name: voicekit
    auto_answer: true
    auto_join_group_calls: false
    allowed_users: []

  discord:
    enabled: false
    bot_token: ${DISCORD_BOT_TOKEN}
    auto_join_channels: []
    command_prefix: "!vk"
    guild_ids: []
    listen_to_all_users: true

  zoom:
    enabled: false
    client_id: ""
    client_secret: ""

  whatsapp:
    enabled: false
    auto_answer: true
    allowed_contacts: []

  signal:
    enabled: false
    auto_answer: true
    allowed_contacts: []

  slack:
    enabled: false
    bot_token: ${SLACK_BOT_TOKEN}
    app_token: ${SLACK_APP_TOKEN}

  sip:
    enabled: false
    server: ""
    username: ""
    password: ""
    port: 5060
    auto_answer: true
    allowed_numbers: []
"""

    output.write_text(example_config)
    console.print(f"[green]Config file created:[/green] {output}")
    console.print("\nNext steps:")
    console.print("1. Set your [bold]OPENAI_API_KEY[/bold] environment variable")
    console.print("2. Edit the config to enable desired platforms")
    console.print("3. Run [bold]voicekit start[/bold]")


@app.command()
def test(
    platform: str = typer.Argument(help="Platform to test (e.g., virtual_audio, telegram)."),
    config: Path = typer.Option(
        DEFAULT_CONFIG_PATH,
        "--config",
        "-c",
        help="Path to configuration file.",
    ),
) -> None:
    """Test a specific platform connection."""
    from voicekit.config import load_config

    if not config.exists():
        console.print(f"[red]Config file not found:[/red] {config}")
        raise typer.Exit(1)

    cfg = load_config(config)

    async def _test() -> None:
        from voicekit.daemon import _create_platforms

        console.print(f"Testing platform: [bold]{platform}[/bold]")

        # Temporarily enable the platform for testing
        platform_config = getattr(cfg.platforms, platform, None)
        if platform_config is None:
            console.print(f"[red]Unknown platform:[/red] {platform}")
            console.print(
                "Available: virtual_audio, telegram, discord, zoom, "
                "whatsapp, signal, slack, sip"
            )
            return

        platforms = _create_platforms(cfg)
        target = None
        for p in platforms:
            if p.name == platform:
                target = p
                break

        if target is None:
            console.print(
                f"[yellow]Platform '{platform}' is not enabled in config.[/yellow]\n"
                f"Enable it in {config} and try again."
            )
            return

        try:
            await target.start()
            console.print(f"[green]✓[/green] Platform '{platform}' started successfully")

            await asyncio.sleep(2)

            await target.stop()
            console.print(f"[green]✓[/green] Platform '{platform}' stopped cleanly")

        except Exception as exc:
            console.print(f"[red]✗[/red] Platform test failed: {exc}")

    asyncio.run(_test())


@app.command()
def version() -> None:
    """Show the VoiceKit version."""
    console.print(f"VoiceKit v{__version__}")


if __name__ == "__main__":
    app()
