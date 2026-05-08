---
name: no-passthrough-methods
description: Prevent meaningless wrapper methods that only call another method.
---

# No Passthrough Methods

## Rule

Do not create methods that only delegate to another method without adding behavior.

Bad example:

```dart
WkSession? readSessionFromHiveSync() {
  return getSessionSync();
}
```

## Preferred patterns

1. Keep one canonical method and delete the duplicate wrapper.
2. If two names are needed for API compatibility, move one to extension/helper with explicit deprecation and TODO removal date.
3. If wrapper is unavoidable, it must add value (validation, logging, fallback, conversion, error mapping).

## Refactor checklist

1. Search for one-line delegating methods.
2. Replace call sites with canonical method.
3. Remove redundant interface members.
4. Run analyzer on changed files.
