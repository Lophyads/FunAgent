"""Tool-free planning strategy using the shared model runtime."""
from ..frame.conf_mgr import ConfMgr
from ..model.protocols import ModelStrategy, FallbackRule


class MemoryRetrievalStrategy(ModelStrategy):
    @staticmethod
    def SelectModel():
        return ConfMgr.GetInstance().Memory().Policy("recall")["profile"]

    @staticmethod
    def MaxIterations():
        return 1

    @staticmethod
    def ToolSet():
        return ()

    @staticmethod
    def FallbackPolicy():
        profile = ConfMgr.GetInstance().Model().Profile(MemoryRetrievalStrategy.SelectModel())
        return FallbackRule(profile.fallback_profiles)
