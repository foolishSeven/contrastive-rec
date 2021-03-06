import numpy as np
import torch.nn as nn
import torch
import torch.nn.functional as fun
from .base_model import Model
from tensorboardX import SummaryWriter


# 没有想到好的名字就用contrastive recommendation来代表这个model吧
class CR(Model):
    """"""
    def __init__(self, args, dataset, filename):
        super(CR, self).__init__()
        self.args = args
        self.batch_size = self.args.bsz
        self.filename = filename
        self.data_list = dataset
        self.user_size = len(self.data_list.user_set)
        self.item_size = len(self.data_list.item_set)
        self.sz = self.data_list.train_list.shape[0]

        # user id embedding
        self.user_matrix = nn.Embedding(self.user_size, self.args.dim)
        self.user_matrix = self.user_matrix.cuda()
        self.user_matrix = nn.init.normal_(self.user_matrix.weight, std=0.01)

        # item id embedding
        self.item_matrix = nn.Embedding(self.item_size, self.args.dim)
        self.item_matrix = self.item_matrix.cuda()
        self.item_matrix = nn.init.normal_(self.item_matrix.weight, std=0.01)

        # item feature vectors 需要对这个加一层mlp 和 relu函数，使输出后的结果尽可能靠近embedding
        # positive sample 是 item_matrix里面
        self.item_features = torch.from_numpy(self.data_list.item_set)
        self.item_features.requires_grad = False
        self.item_features = self.item_features.cuda()
        dim_mlp = self.item_features.shape[1]
        self.item_features = self.item_features.float()
        self.item_mlp = nn.Sequential(nn.Linear(dim_mlp, dim_mlp), nn.LeakyReLU(), nn.Linear(dim_mlp, dim_mlp))  #
        self.item_mlp = self.item_mlp.cuda()

    def predict(self, uid, iid):
        """
        uid of user_matrix
        iid of item_matrix
        :return:
        """
        p1 = self.user_matrix[uid]
        p2 = self.item_matrix[iid]
        return torch.sum(p1 * p2, dim=1)

    def predict_cold(self, uid, iid):
        """
        uid of user_matrix
        iid of item_matrix
        :return:
        """
        p1 = self.user_matrix[uid]
        item_fixed = self.item_mlp(self.item_features)
        p2 = item_fixed[iid]
        return torch.sum(p1 * p2, dim=1)

    def bpr_loss(self, uid, iid, jid):
        """
        bpr的算法是，对一个用户u求i和j两个item的分数，然后比较更喜欢哪个，
        所以这里需要进行两次预测，分别是第i个item的和第j个item的
        """
        pre_i = self.predict(uid, iid)
        pre_j = self.predict(uid, jid)
        dev = pre_i - pre_j
        return torch.sum(fun.softplus(-dev))

    def con_loss(self, item_fixed, uid, iid, jid):
        """
        feature and item embedding element wise user_matrix then compute 余弦距离 用log公式求loss
        """
        pos_ids = torch.unique(iid)
        neg_ids = torch.unique(jid)
        anchor_feature = torch.einsum('ni, ci -> nc', [item_fixed[pos_ids], self.user_matrix[uid]])  # 得到 is * us
        pos_embedding = torch.einsum('ni, ci -> nc', [self.item_matrix[pos_ids], self.user_matrix[uid]])  # is * us
        neg_embedding = torch.einsum('ni, ci -> nc', [self.item_matrix[neg_ids], self.user_matrix[uid]])  # 得到 js * us
        anchor_feature_squeeze = anchor_feature.unsqueeze(1)  # 升维用于跟 neg_embedding的广播机制运算 点乘

        anchor_fea_norm = torch.norm(anchor_feature, dim=1, keepdim=True)  # 求二范数
        pos_emb_norm = torch.norm(pos_embedding, dim=1, keepdim=True)  # 求二范数
        neg_emb_norm = torch.norm(neg_embedding, dim=1, keepdim=True)  # 求二范数

        norms_pos = torch.einsum('ni, ni -> n', [anchor_fea_norm, pos_emb_norm])  # anchor和pos_emb 的二范数点乘
        pos_scores = torch.einsum('ni, ni -> n', [anchor_feature, pos_embedding]) / norms_pos  # anchor和pos的余弦距离
        pos_scores_exp = torch.exp(pos_scores)  # 按照公式，对其做exp，如果有负数求log的时候就nan了

        neg_fea_mul = anchor_feature_squeeze * neg_embedding  # anchor和neg embedding的element wise乘
        norms_neg = torch.einsum('ni, ci -> nc', [anchor_fea_norm, neg_emb_norm])  # 二范数的乘积
        neg_scores = torch.sum(neg_fea_mul, dim=-1) / norms_neg  # anchor和neg embedding的余弦距离
        neg_scores_exp = torch.exp(neg_scores)
        neg_scores_exp_sum = torch.sum(neg_scores_exp, dim=1)

        """
          对于每一个item求 -log(pos / (pos + neg))， 然后所有item加和
        """
        score_mul = pos_scores_exp / (pos_scores_exp + neg_scores_exp_sum)
        log_gits = -torch.log(score_mul)
        contrastive_loss = torch.sum(log_gits)

        return contrastive_loss

    def con_loss_matmul(self, item_fixed, iid, jid):
        pos_ids = torch.unique(iid)
        neg_ids = torch.unique(jid)
        pos_feature = item_fixed[pos_ids]  # 得到 is * 64矩阵
        pos_embedding = self.item_matrix[pos_ids]
        neg_embedding = self.item_matrix[neg_ids]  # 得到 js * 64矩阵
        pos_pos_dif = pos_feature - pos_embedding
        pos_scores = torch.einsum('ni, ni -> n', [pos_pos_dif, pos_pos_dif])  # 得到1 * is 的矩阵
        pos_feature_squeeze = pos_feature.unsqueeze(1)  # 将pos_feature升维成is * 1 * 64方便广播计算
        pos_neg_dif = pos_feature_squeeze - neg_embedding  # 得到的差值是一个 is * js * 64的矩阵
        neg_scores = torch.einsum('nij, nij -> ni', [pos_neg_dif, pos_neg_dif])  # 得到 is * js 的平方和矩阵 i 与 j个neg
        neg_scores_sum = torch.sum(neg_scores, dim=1)  # 得到 1 * is的矩阵
        """
          对于每一个item求 -log(pos / (pos + neg))， 然后所有item加和
        """
        contrastive_loss = torch.sum(-torch.log(pos_scores / (pos_scores + neg_scores_sum)))

        return contrastive_loss * 0.07

    def regs(self, uid, iid, jid):
        # regs:  default value is 0
        reg = self.args.reg
        uid_v = self.user_matrix[uid]
        iid_v = self.item_matrix[iid]
        jid_v = self.item_matrix[jid]
        emb_regs = torch.sum(uid_v * uid_v) + torch.sum(iid_v * iid_v) + torch.sum(jid_v * jid_v)
        return reg * emb_regs

    def train(self):

        print('cr is training')
        lr = self.args.lr
        optimizer = torch.optim.Adam(self.parameters(), lr=lr, weight_decay=0)
        epochs = self.args.epochs
        for epoch in range(epochs):

            generator = self.sample()  # 这里生成的
            # current_loop = 1
            while True:

                optimizer.zero_grad()
                item_fixed = self.item_mlp(self.item_features)

                s = next(generator)
                if s is None:
                    break

                uid, iid, jid = s[:, 0], s[:, 1], s[:, 2]
                uid = uid.cuda()
                iid = iid.cuda()
                jid = jid.cuda()
                loss_bpr = self.bpr_loss(uid, iid, jid) + self.regs(uid, iid, jid)
                # loss_con = self.con_loss_matmul(item_fixed, iid, jid)
                loss_con = self.con_loss(item_fixed, uid, iid, jid)  # con_loss 训练到0.00应该是显然存在问题的
                loss = loss_con + loss_bpr
                # current_loop += 1

                loss.backward()
                optimizer.step()
            # if epoch % 10 == 0 and epoch > 1:
            #     torch.save(item_fixed, f'.\\crpt\\exp9\\epoch{epoch}-item_fixed.pt')

            if epoch % 2 == 0 and epoch > 1:  #
                print(f'=={epoch}===>loss_bpr is {loss_bpr}===loss_con is {loss_con}')
                file_path = open(self.filename, 'a')
                print(f'epoch is {epoch}-----', file=file_path)
                self.val(), self.test(), self.test_warm(), self.test_cold()

    def sample(self):
        np.random.shuffle(self.data_list.train_list)
        loop_size = self.sz // self.batch_size
        for i in range(loop_size):
            pairs = []
            sub_train_list = self.data_list.train_list[i * self.batch_size:(i + 1) * self.batch_size, :]
            for m, j in sub_train_list:
                m_neg = j
                while m_neg in self.data_list.train_pt[m]:
                    m_neg = np.random.randint(self.item_size)
                pairs.append((m, j, m_neg))

            yield torch.LongTensor(pairs)
        yield None
