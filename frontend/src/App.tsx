import { Route, Routes } from 'react-router-dom'
import Layout from './components/Layout'
import Overview from './pages/Overview'
import TraceExplorer from './pages/TraceExplorer'
import TraceDetail from './pages/TraceDetail'
import DagAnalysis from './pages/DagAnalysis'
import FaultLab from './pages/FaultLab'
import CounterfactualLab from './pages/CounterfactualLab'
import RepairStudio from './pages/RepairStudio'
import EvaluationCenter from './pages/EvaluationCenter'
import JudgeCalibration from './pages/JudgeCalibration'
import DisagreementExplorer from './pages/DisagreementExplorer'
import AgentRuns from './pages/AgentRuns'
import TraceComparison from './pages/TraceComparison'
import FaultAttribution from './pages/FaultAttribution'
import RedTeamLab from './pages/RedTeamLab'
import AdversarialLab from './pages/AdversarialLab'
import VersionComparison from './pages/VersionComparison'
import EvaluationPipeline from './pages/EvaluationPipeline'
import CostAnalytics from './pages/CostAnalytics'
import ParetoExplorer from './pages/ParetoExplorer'
import Architecture from './pages/Architecture'

export default function App() {
  return (
    <Routes>
      <Route element={<Layout />}>
        <Route index element={<Overview />} />
        <Route path="/traces" element={<TraceExplorer />} />
        <Route path="/traces/:traceId" element={<TraceDetail />} />
        <Route path="/redteam" element={<RedTeamLab />} />
        <Route path="/adversarial" element={<AdversarialLab />} />
        <Route path="/architecture" element={<Architecture />} />
        <Route path="/dag" element={<DagAnalysis />} />
        <Route path="/dag/:traceId" element={<DagAnalysis />} />
        <Route path="/runs" element={<AgentRuns />} />
        <Route path="/fault-lab" element={<FaultLab />} />
        <Route path="/counterfactual" element={<CounterfactualLab />} />
        <Route path="/repair" element={<RepairStudio />} />
        <Route path="/evaluation" element={<EvaluationCenter />} />
        <Route path="/version-compare" element={<VersionComparison />} />
        <Route path="/pipelines" element={<EvaluationPipeline />} />
        <Route path="/costs" element={<CostAnalytics />} />
        <Route path="/pareto" element={<ParetoExplorer />} />
        <Route path="/calibration" element={<JudgeCalibration />} />
        <Route path="/multi-judge" element={<DisagreementExplorer />} />
        <Route path="/comparison" element={<TraceComparison />} />
        <Route path="/attribution" element={<FaultAttribution />} />
      </Route>
    </Routes>
  )
}
