"""Autonomous acquisition and platform improvement (V1, template-only).

Concrete V1 implementations of the normative protocols in ``reva.ports.autonomy``:

=============================  ==============================================
Protocol                       Implementation
=============================  ==============================================
SourceDiscoverer               discovery.EurostatCatalogueDiscoverer
OntologyMapper                 mapping.RegistryOntologyMapper
ConnectorGenerator             generator.DeclarativeConnectorGenerator
ConnectorValidator             validator.JsonStatConnectorValidator
ConnectorHealthMonitor         health.RunHistoryHealthMonitor
EvidenceExtractor              evidence.StructuredCardImporter,
                               evidence.LlmEvidenceExtractor (blocked by default)
DataQualityEvaluator           quality.DimensionRuleQualityEvaluator
ModelRegistry                  models.PostgresModelRegistry
ModelEvaluator                 models.TournamentModelEvaluator
BacktestRunner                 models.TournamentBacktestRunner
KnowledgeGapDetector           gaps.CatalogueGapDetector
ValueOfInformationEstimator    voi.GapValueEstimator
ModelChallenger                challenger.BoundedModelChallenger
PlatformChangeProposal         proposals.ProposalStore (ops.proposal persistence)
SandboxRunner                  sandbox.TrustedTemplateSandbox (+ ExternalRunner)
PromotionGate                  gate.PolicyPromotionGate
=============================  ==============================================

Job handlers: ``reva.autonomy.jobs:handle_discovery`` and ``reva.autonomy.jobs:handle_gaps``.
"""
