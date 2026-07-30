# Android shell (Trusted Web Activity)

A ~1 MB APK that opens `https://kana.rjp.io` full-screen with no browser UI. It
is **not** a port — Chrome renders the same site, so `bin/sync kana-quiz`
deploys still go live instantly and the APK only needs rebuilding when
something in `twa-manifest.json` changes (name, icon, colours, signing key).

Because a TWA runs on Chrome's engine it shares Chrome's cookie jar, so the
`kana_auth` cookie carries over — if you're logged in in Chrome, you're logged
in in the app.

## Why the URL bar might appear

Chrome hides the address bar only if it can verify the app owns the domain. It
checks `https://kana.rjp.io/.well-known/assetlinks.json` for the SHA-256
fingerprint of the APK's signing certificate. That file lives at
`frontend/public/.well-known/assetlinks.json` and ships with the normal
frontend build.

**If the signing key and that file ever disagree, you get a URL bar and no
error message.** Verify them against each other:

    apksigner verify --print-certs app-release-signed.apk | grep -i 'SHA-256'
    cat ../frontend/public/.well-known/assetlinks.json

Chrome caches the verification result, so after changing `assetlinks.json`
reinstall the app rather than expecting it to notice.

## The signing key

Lives outside the repo at `~/.android-keys/kana-quiz.keystore` (alias `kana`,
password in `kana-quiz.password` alongside it, both mode 600). It is covered by
the homedir restic backup.

Losing it is recoverable but annoying: generate a new key, regenerate
`assetlinks.json`, redeploy, then uninstall and reinstall the app — Android
refuses to upgrade an installed app whose signature changed.

## One-time toolchain setup

    sudo apt-get install -y openjdk-17-jdk-headless
    npm install -g @bubblewrap/cli

    # Android SDK — cmdline-tools go under $SDK/cmdline-tools/latest
    SDK=~/Android/sdk && mkdir -p $SDK/cmdline-tools
    curl -sL -o /tmp/cmdline-tools.zip \
      https://dl.google.com/android/repository/commandlinetools-linux-15859902_latest.zip
    unzip -q /tmp/cmdline-tools.zip -d $SDK/cmdline-tools
    mv $SDK/cmdline-tools/cmdline-tools $SDK/cmdline-tools/latest
    export ANDROID_HOME=$SDK PATH=$SDK/cmdline-tools/latest/bin:$PATH
    yes | sdkmanager --licenses
    sdkmanager platform-tools 'platforms;android-35' 'build-tools;34.0.0'

    # Bubblewrap expects the pre-6858069 layout: sdkmanager directly under
    # $SDK/bin, not $SDK/cmdline-tools/latest/bin. Bridge it.
    ln -sfn $SDK/cmdline-tools/latest/bin $SDK/bin
    ln -sfn $SDK/cmdline-tools/latest/lib $SDK/lib

    # Skips Bubblewrap's interactive first-run setup.
    mkdir -p ~/.bubblewrap && cat > ~/.bubblewrap/config.json <<'JSON'
    {
      "jdkPath": "/usr/lib/jvm/java-17-openjdk-amd64",
      "androidSdkPath": "/home/power/Android/sdk"
    }
    JSON

Bubblewrap pins `build-tools;34.0.0` (`BUILD_TOOLS_VERSION` in its
`AndroidSdkTools.js`) — installing only 35.0.0 fails with a confusing error.

## Build

    cd android
    export JAVA_HOME=/usr/lib/jvm/java-17-openjdk-amd64
    export ANDROID_HOME=~/Android/sdk
    export BUBBLEWRAP_KEYSTORE_PASSWORD="$(cat ~/.android-keys/kana-quiz.password)"
    export BUBBLEWRAP_KEY_PASSWORD="$BUBBLEWRAP_KEYSTORE_PASSWORD"
    bubblewrap build --skipPwaValidation

Produces `app-release-signed.apk` (sideload this) and `app-release-bundle.aab`
(only needed for Play Store upload, which we don't do). Both are gitignored.

`bubblewrap build` refuses to run if `twa-manifest.json` changed since the last
project generation — run `bubblewrap update --skipVersionUpgrade` first. Those
two env vars are what keep the build non-interactive; without them it prompts
for the keystore password.

## Install

    adb install -r app-release-signed.apk

or copy the APK to the phone and open it (needs "install unknown apps" for
whatever app you opened it from). Bumping `appVersionCode` in
`twa-manifest.json` is only needed for the Play Store; sideloaded reinstalls
over the top work regardless as long as the signature matches.
