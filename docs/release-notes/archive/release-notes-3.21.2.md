# la musica 3.21.2 — the iOS check that failed a good bundle

3.21.1's workflow ran the whole pipeline and stopped at the iOS job: the step
that proves the installed bundle still declares background audio reported

```
UIBackgroundModes: 1
::error::UIBackgroundModes does not carry 'audio' — playback would stop as soon as the app leaves the foreground
```

against a plist that was completely correct. `plutil -extract <key> raw` prints
an array's **count**, not its contents — so the check was reading `1` and calling
it a missing declaration. The same step's other two reads were right, and they
said exactly what they should: `NSAllowsArbitraryLoadsInWebContent: true` and
`CFBundleDisplayName: la musica`.

The step now prints the array itself (PlistBuddy, one indented element per line)
and matches the `audio` element anywhere in it, so neither the array's form nor
the order of its elements can fail the build.

Worth saying plainly: the plists were never wrong. `UIBackgroundModes` carried
its one element the whole time — the count of 1 is what the old check tripped
over. The iPhone-side claim (#47) stands on what the bundle declares, and this
step is what keeps it declared.

## Upgrading

Nothing to do. This is 3.21.1 — which is 3.21.0 plus one test fix — with a
working iOS verification step.
