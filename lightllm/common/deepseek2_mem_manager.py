import torch
import os

from lightllm.server.pd_io_struct import KVMoveTask
from .mem_manager import MemoryManager
from typing import List


class Deepseek2MemoryManager(MemoryManager):
    def get_cell_size(self):
        return self.head_num * self.head_dim * self.layer_num * torch._utils._element_size(self.dtype)

    def _init_buffers(self, size, dtype, head_num, head_dim, layer_num):
        self.kv_buffer = torch.empty((layer_num, size, head_num, head_dim), dtype=dtype, device="cuda")
        # todo, etp or edp use the same work buffer here
        # also it can be used for any kernels for work buffer witout save info only
        if os.environ.get("ETP_MODE_ENABLED") == "true" or os.environ.get("EDP_MODE_ENABLED") == "true":
            self.work_buffer = torch.empty(1024 * 1024 * 1024, dtype=torch.bfloat16, device="cuda")
            self.work_buffer.share_memory_()
            import lightllm_moe_etp_kernel
            import torch.distributed as dist

            rank_id = dist.get_rank()
            world_size = dist.get_world_size()

            # lightllm_moe_etp_kernel.enableP2P(world_size, rank_id)

            handle = lightllm_moe_etp_kernel.get_handle(self.work_buffer.contiguous(), rank_id)
            handles = [None] * world_size
            dist.all_gather_object(handles, handle)
            self.handles_work_buffer = handles

            lightllm_moe_etp_kernel.init_system(world_size, rank_id, self.work_buffer.contiguous(), handles)

    def alloc_kv_move_buffer(self, max_req_total_len):
        self.kv_move_buffer = torch.empty(
            (1, max_req_total_len + 8, self.head_num, self.head_dim), dtype=self.dtype, device="cuda"
        )
        return

    def send_to_decode_node(
        self, move_tasks: List[KVMoveTask], mem_managers: List["Deepseek2MemoryManager"], dp_size: int
    ):
        assert dp_size == 1

        # 先将数据发送到指定的一张卡上的buffer，再发送。
        import torch.distributed as dist

        move_token_indexes = []
        for task in move_tasks:
            if task.move_kv_len != 0:
                move_token_indexes.extend(task.prefill_token_indexes[-task.move_kv_len :])

        cur_device_index = self.kv_buffer.get_device()
        cur_mem = mem_managers[cur_device_index]
        for layer_index in range(cur_mem.layer_num):
            move_buffer = cur_mem._get_kv_move_data(move_token_indexes, layer_index)
            dist.send(move_buffer, dst=1)
        return

    def _get_kv_move_data(self, token_indexes: List[int], layer_index: int):
        move_size = self.kv_buffer.numel() // self.layer_num // self.size * len(token_indexes)
        move_buffer = self.kv_move_buffer.view(-1)[0:move_size].view(
            1, len(token_indexes), self.head_num, self.head_dim
        )
        move_buffer[:, :, :, :] = self.kv_buffer[layer_index, token_indexes, :, :]
        return move_buffer

    def receive_from_prefill_node(
        self, move_tasks: List[KVMoveTask], mem_managers: List["MemoryManager"], dp_size: int
    ):
        assert dp_size == 1

        # 先将数据接受到指定的一张卡上的buffer，再复制到其他的卡上。
        import torch.distributed as dist

        move_token_indexes = []
        for task in move_tasks:
            if task.move_kv_len != 0:
                move_token_indexes.extend(task.decode_token_indexes[-task.move_kv_len :])

        cur_device_index = self.kv_buffer.get_device()
        token_num = len(move_token_indexes)
        move_size = self.kv_buffer.numel() // self.layer_num // self.size * token_num
        recive_buffer = self.kv_move_buffer.view(-1)[0:move_size].view(1, token_num, self.head_num, self.head_dim)
        for layer_index in range(self.layer_num):
            dist.recv(recive_buffer, src=0)
            for i, mem in enumerate(mem_managers):
                if i == cur_device_index:
                    mem._write_kv_move_data(move_token_indexes, recive_buffer, layer_index)
                else:
                    new_recive_buffer = mem.kv_move_buffer.view(-1)[0:move_size].view(recive_buffer.shape)
                    from torch.cuda import comm

                    comm.broadcast(recive_buffer, out=[new_recive_buffer])
                    mem._write_kv_move_data(move_token_indexes, new_recive_buffer, layer_index)
        return

    def _write_kv_move_data(self, token_indexes: torch.Tensor, buffer_tensor: torch.Tensor, layer_index):
        self.kv_buffer[layer_index : layer_index + 1, token_indexes, :, :] = buffer_tensor
        return
