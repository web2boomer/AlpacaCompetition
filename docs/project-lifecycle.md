# Project lifecycle

Alpaca Competition is **In Development** (`in_development`) in Mission Control's
canonical `config/projects.yaml` registry. The prior competition runtime and historical
deployment do not make the project an active Live product.

CI remains one offline verification job and now runs feature changes through pull requests
only, preserves a main-branch audit run, caches dependencies, and cancels superseded PR runs.
The lifecycle label does not authorize broker activity, deployment, or reuse of expired
competition authorities.
