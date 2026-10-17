"""V6 PRED-MODELS: real-time (vintage) evaluation of forecasting models against strong benchmarks.

* :mod:`.realtime` — forecast cases built origin by origin from :class:`reva.evaluation.vintages.VintageAccessor`
  (IMF WEO panel, US RTDSM + SPF, G7 OECD MEI real-time editions);
* :mod:`.derived` — second pass: combinations, conformal densities and bias corrections that use only
  out-of-sample errors whose outcomes were published at the origin;
* :mod:`.gate` — proper scores, Diebold-Mariano/HLN by origin, Holm, the development / locked-holdout protocol;
* :mod:`.service` — loads the vintage tables, runs a stage, stores the manifest, keeps the holdout ledger.
"""
