#!/usr/bin/env python3
"""
TrainChimp Fine-Tuning Service
------------------------------
A service that watches Cloudflare queue for fine-tuning tasks,
loads specified models, and performs LoRA fine-tuning.
"""

import os
import json
import time
import logging
import requests
import boto3
from typing import Dict, Any, Optional
from datetime import datetime

# ML imports
import torch
from transformers import (
    AutoModelForCausalLM, 
    AutoTokenizer,
    TrainingArguments,
    Trainer,
    DataCollatorForLanguageModeling
)
from peft import (
    get_peft_model,
    LoraConfig, 
    TaskType,
    prepare_model_for_kbit_training
)
from datasets import load_dataset
from supabase import create_client, Client

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

class FineTuningService:
    """Service to manage fine-tuning jobs"""
    
    def __init__(self, supabase_client: Client, data_dir: str = "/tmp/trainchimp"):
        self.supabase_client = supabase_client
        self.data_dir = data_dir
        
        # Create directories
        os.makedirs(self.data_dir, exist_ok=True)
        os.makedirs(os.path.join(self.data_dir, "datasets"), exist_ok=True)
        os.makedirs(os.path.join(self.data_dir, "models"), exist_ok=True)
        
        # Models and tokenizers will be loaded as needed
        self.models = {}
        self.tokenizers = {}
    
    def load_base_model(self, base_model_type):
        """Load a base model into memory"""
        logger.info(f"Loading base model: {base_model_type}")
        
        # Check for GPU
        device = "cuda" if torch.cuda.is_available() else "cpu"
        logger.info(f"Using device: {device}")
        
        # Load tokenizer if not already loaded
        if base_model_type not in self.tokenizers:
            logger.info(f"Loading tokenizer for {base_model_type}")
            self.tokenizers[base_model_type] = AutoTokenizer.from_pretrained(base_model_type)
        
        # Load model in 4-bit to save memory
        model = AutoModelForCausalLM.from_pretrained(
            base_model_type,
            torch_dtype=torch.float16,
            load_in_4bit=True,
            device_map="auto"
        )
        
        # Prepare the model for training
        model = prepare_model_for_kbit_training(model)
        
        self.models[base_model_type] = model
        logger.info(f"Base model loaded successfully")
    
    def reset_model(self, base_model_type):
        """Reset a specific model by clearing it from memory"""
        logger.info(f"Resetting model: {base_model_type}")
        if base_model_type in self.models:
            del self.models[base_model_type]
            torch.cuda.empty_cache()
    
    def process_dataset(self, dataset_path, base_model_type, instruction_template=None):
        """Process the dataset for training"""
        logger.info(f"Processing dataset: {dataset_path}")
        
        # Ensure tokenizer is loaded
        if base_model_type not in self.tokenizers:
            logger.info(f"Loading tokenizer for {base_model_type}")
            self.tokenizers[base_model_type] = AutoTokenizer.from_pretrained(base_model_type)
        
        tokenizer = self.tokenizers[base_model_type]
        
        # Load dataset
        dataset = load_dataset('json', data_files=dataset_path)
        
        # Apply tokenization
        def tokenize_function(examples):
            # Format based on instruction template or default to raw text
            if instruction_template:
                texts = [instruction_template.format(**item) for item in zip(
                    examples.get('instruction', ['']),
                    examples.get('input', ['']),
                    examples.get('output', [''])
                )]
            else:
                # Fallback to using 'text' field if available
                texts = examples.get('text', examples.get('content', []))
            
            return tokenizer(
                texts, 
                truncation=True, 
                padding="max_length",
                max_length=512
            )
        
        # Tokenize the dataset
        tokenized_dataset = dataset.map(
            tokenize_function,
            batched=True,
            remove_columns=dataset["train"].column_names
        )
        
        return tokenized_dataset["train"]
    
    def fine_tune(self, job_id):
        """Fine-tune the model with LoRA based on job specifications"""
        # Get job data from Supabase
        job_data = self.supabase_client.get_job(job_id)
        if not job_data:
            logger.error(f"Job {job_id} not found")
            return False
            
        model_id = job_data["model_id"]
        dataset_id = job_data["dataset_id"]
        base_model = job_data["base_model"]
        training_params = job_data["training_params"]
        
        # Update job status
        started_at = datetime.now().isoformat()
        self.supabase_client.update_job_status(
            job_id, 
            "running",
            started_at=started_at
        )
        self.supabase_client.update_model_status(model_id, "training")
        
        try:
            # Download dataset
            dataset_path = self.supabase_client.download_dataset(
                dataset_id,
                os.path.join(self.data_dir, "datasets")
            )
            
            # Load base model if not already loaded
            if base_model not in self.models:
                self.load_base_model(base_model)
            
            model = self.models[base_model]
            tokenizer = self.tokenizers[base_model]
            
            # Process dataset
            train_dataset = self.process_dataset(dataset_path, base_model)
            
            # Configure LoRA
            lora_config = LoraConfig(
                r=training_params.get("lora_rank", 8),
                lora_alpha=training_params.get("lora_alpha", 16),
                task_type=TaskType.CAUSAL_LM,
                lora_dropout=0.05,
                bias="none",
                target_modules=["q_proj", "v_proj", "k_proj", "o_proj", "gate_proj", "up_proj", "down_proj"]
            )
            
            # Apply LoRA config to the model
            peft_model = get_peft_model(model, lora_config)
            
            # Setup training arguments
            output_dir = os.path.join(self.data_dir, "models", model_id)
            training_args = TrainingArguments(
                output_dir=output_dir,
                num_train_epochs=training_params.get("epochs", 3),
                per_device_train_batch_size=training_params.get("batch_size", 8),
                gradient_accumulation_steps=4,
                learning_rate=training_params.get("learning_rate", 2e-5),
                bf16=True if torch.cuda.is_available() else False,
                save_strategy="epoch",
                logging_steps=10,
                save_total_limit=1,
                save_safetensors=True,
            )
            
            # Setup data collator
            data_collator = DataCollatorForLanguageModeling(
                tokenizer=tokenizer, 
                mlm=False
            )
            
            # Initialize trainer
            trainer = Trainer(
                model=peft_model,
                args=training_args,
                train_dataset=train_dataset,
                data_collator=data_collator,
            )
            
            # Start training
            logger.info(f"Starting fine-tuning for model {model_id}")
            trainer.train()
            
            # Save the trained model
            peft_model.save_pretrained(output_dir)
            
            # Upload the model to storage
            adapter_url = self.supabase_client.upload_model(
                model_id,
                output_dir
            )
            
            # Update job and model status
            completed_at = datetime.now().isoformat()
            self.supabase_client.update_job_status(
                job_id, 
                "completed",
                completed_at=completed_at
            )
            self.supabase_client.update_model_status(
                model_id, 
                "ready",
                lora_adapter_url=adapter_url
            )
            
            logger.info(f"Fine-tuning completed for model {model_id}")
            return True
            
        except Exception as e:
            logger.error(f"Error during fine-tuning: {e}", exc_info=True)
            
            # Update job and model status
            self.supabase_client.update_job_status(job_id, "failed")
            self.supabase_client.update_model_status(model_id, "failed")
            
            return False
        finally:
            # Reset the model to free up memory
            self.reset_model(base_model)
    
    def run(self, job_id):
        """Run the service for a specific job ID"""
        logger.info(f"Starting fine-tuning service for job {job_id}")
        return self.fine_tune(job_id)


class SupabaseClient:
    """Client for interacting with Supabase"""
    
    def __init__(self, url: str, key: str):
        self.client = create_client(url, key)
    
    def get_job(self, job_id: str) -> Optional[Dict[str, Any]]:
        """Get job data from Supabase"""
        response = self.client.table('jobs').select('*').eq('job_id', job_id).execute()
        jobs = response.data
        if not jobs:
            return None
        return jobs[0]
    
    def update_job_status(self, job_id: str, status: str, started_at: str = None, completed_at: str = None):
        """Update job status in Supabase"""
        data = {'status': status}
        if started_at:
            data['started_at'] = started_at
        if completed_at:
            data['completed_at'] = completed_at
            
        self.client.table('jobs').update(data).eq('job_id', job_id).execute()
    
    def update_model_status(self, model_id: str, status: str, lora_adapter_url: str = None):
        """Update model status in Supabase"""
        data = {'status': status}
        if lora_adapter_url:
            data['lora_adapter_url'] = lora_adapter_url
            
        self.client.table('models').update(data).eq('id', model_id).execute()
    
    def download_dataset(self, dataset_id: str, destination_dir: str) -> str:
        """Download dataset from storage"""
        dataset_info = self.client.table('datasets').select('*').eq('id', dataset_id).execute().data[0]
        dataset_url = dataset_info.get('file_url')
        
        if not dataset_url:
            raise ValueError(f"Dataset {dataset_id} has no file URL")
        
        local_path = os.path.join(destination_dir, f"{dataset_id}.jsonl")
        
        # Download file
        response = requests.get(dataset_url)
        response.raise_for_status()
        
        with open(local_path, 'wb') as f:
            f.write(response.content)
        
        return local_path
    
    def upload_model(self, model_id: str, model_dir: str) -> str:
        """Upload model files and return the URL"""
        # Implementation will depend on storage solution
        # This is a placeholder - actual implementation would need to zip and upload files
        
        # For example, upload to Supabase storage
        bucket_name = "model-adapters"
        file_path = os.path.join(model_dir, "adapter_model.safetensors")
        
        with open(file_path, 'rb') as f:
            self.client.storage.from_(bucket_name).upload(
                f"{model_id}/adapter_model.safetensors",
                f.read()
            )
        
        # Return URL to the uploaded model
        return self.client.storage.from_(bucket_name).get_public_url(f"{model_id}/adapter_model.safetensors")


def main():
    """Main entry point"""
    # Get job ID from environment
    job_id = os.environ.get("JOB_ID")
    if not job_id:
        logger.error("JOB_ID must be set in environment")
        return 1
    
    # Try to get Supabase credentials
    supabase_url = os.environ.get("SUPABASE_URL")
    supabase_key = os.environ.get("SUPABASE_KEY")
    
    if not supabase_url or not supabase_key:
        logger.error("SUPABASE_URL and SUPABASE_KEY must be set in environment")
        return 1
    
    logger.info("Initializing Supabase client")
    supabase_client = SupabaseClient(supabase_url, supabase_key)
    
    # Get data directory from environment or use default
    data_dir = os.environ.get("DATA_DIR", "/tmp/trainchimp")
    
    # Initialize and run the service
    service = FineTuningService(supabase_client, data_dir)
    
    # Run the service with the specified job ID
    success = service.run(job_id)
    
    return 0 if success else 1


if __name__ == "__main__":
    main() 