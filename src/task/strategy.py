from ..frame.conf import ConfMgr
from ..model import ModelStrategy, FallbackRule

class MainStrategy(ModelStrategy):
    @staticmethod
    def SelectModel():
        return ConfMgr.GetInstance().Serv().Options()["model_profile"]

    @staticmethod
    def MaxIterations():
        return ConfMgr.GetInstance().Serv().Options().get("max_iterations", 6)

    @staticmethod
    def ToolSet():
        return ConfMgr.GetInstance().Tool().Selection("main")

    @staticmethod
    def FallbackPolicy():
        profile = ConfMgr.GetInstance().Model().Profile(MainStrategy.SelectModel())
        return FallbackRule(profile.fallback_profiles)


