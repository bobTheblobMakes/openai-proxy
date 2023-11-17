import sys, os, platform, time, copy
import threading, ast
import shutil, random, traceback, requests
from typing import Optional
messages: list = []
sys.path.insert(
    0, os.path.abspath("../..")
)  # Adds the parent directory to the system path - for litellm local dev

try:
    import uvicorn
    import fastapi
    import tomli as tomllib
    import appdirs
    import tomli_w
    import backoff
    import yaml
except ImportError:
    import subprocess
    import sys

    subprocess.check_call(
        [
            sys.executable,
            "-m",
            "pip",
            "install",
            "uvicorn",
            "fastapi",
            "tomli",
            "appdirs",
            "tomli-w",
            "backoff",
            "pyyaml"
        ]
    )
    import uvicorn
    import fastapi
    import tomli as tomllib
    import appdirs
    import tomli_w
    import backoff
    import yaml

import random

list_of_messages = [
    "'The thing I wish you improved is...'",
    "'A feature I really want is...'",
    "'The worst thing about this product is...'",
    "'This product would be better if...'",
    "'I don't like how this works...'",
    "'It would help me if you could add...'",
    "'This feature doesn't meet my needs because...'",
    "'I get frustrated when the product...'",
]


def generate_feedback_box():
    box_width = 60

    # Select a random message
    message = random.choice(list_of_messages)

    print()
    print("\033[1;37m" + "#" + "-" * box_width + "#\033[0m")
    print("\033[1;37m" + "#" + " " * box_width + "#\033[0m")
    print("\033[1;37m" + "# {:^59} #\033[0m".format(message))
    print(
        "\033[1;37m"
        + "# {:^59} #\033[0m".format("https://github.com/BerriAI/litellm/issues/new")
    )
    print("\033[1;37m" + "#" + " " * box_width + "#\033[0m")
    print("\033[1;37m" + "#" + "-" * box_width + "#\033[0m")
    print()
    print(" Thank you for using LiteLLM! - Krrish & Ishaan")
    print()
    print()
    print()
    print(
        "\033[1;31mGive Feedback / Get Help: https://github.com/BerriAI/litellm/issues/new\033[0m"
    )
    print()
    print("\033[1;34mDocs: https://docs.litellm.ai/docs/simple_proxy\033[0m\n")
    print(f"\033[32mLiteLLM: Test your local endpoint with: \"litellm --test\" [In a new terminal tab]\033[0m\n")
    print()

import litellm
litellm.suppress_debug_info = True
from fastapi import FastAPI, Request, HTTPException, status, Depends
from fastapi.routing import APIRouter
from fastapi.encoders import jsonable_encoder
from fastapi.responses import StreamingResponse, FileResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import OAuth2PasswordBearer
import json
import logging

app = FastAPI(docs_url="/", title="LiteLLM API")
router = APIRouter()
origins = ["*"]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

user_api_base = None
user_model = None
user_debug = False
user_max_tokens = None
user_request_timeout = None
user_temperature = None
user_telemetry = True
user_config = None
user_headers = None
local_logging = True # writes logs to a local api_log.json file for debugging
config_filename = "litellm.secrets.toml"
config_dir = os.getcwd()
config_dir = appdirs.user_config_dir("litellm")
user_config_path = os.getenv(
    "LITELLM_CONFIG_PATH", os.path.join(config_dir, config_filename)
)
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="token")
experimental = False
#### GLOBAL VARIABLES ####
llm_router: Optional[litellm.Router] = None
llm_model_list: Optional[list] = None
server_settings: dict = {}
log_file = "api_log.json"
worker_config = None
master_key = None
#### HELPER FUNCTIONS ####
def print_verbose(print_statement):
    global user_debug
    if user_debug:
        print(print_statement)


def usage_telemetry(
    feature: str,
):  # helps us know if people are using this feature. Set `litellm --telemetry False` to your cli call to turn this off
    if user_telemetry:
        data = {"feature": feature}  # "local_proxy_server"
        threading.Thread(
            target=litellm.utils.litellm_telemetry, args=(data,), daemon=True
        ).start()

async def user_api_key_auth(request: Request): 
    global master_key
    if master_key is None:
        return 
    try: 
        api_key = await oauth2_scheme(request=request)
        if api_key == master_key: 
            return
    except: 
        pass
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail={"error": "invalid user key"},
    )

def add_keys_to_config(key, value):
    # Check if file exists
    if os.path.exists(user_config_path):
        # Load existing file
        with open(user_config_path, "rb") as f:
            config = tomllib.load(f)
    else:
        # File doesn't exist, create empty config
        config = {}

    # Add new key
    config.setdefault("keys", {})[key] = value

    # Write config to file
    with open(user_config_path, "wb") as f:
        tomli_w.dump(config, f)


def save_params_to_config(data: dict):
    # Check if file exists
    if os.path.exists(user_config_path):
        # Load existing file
        with open(user_config_path, "rb") as f:
            config = tomllib.load(f)
    else:
        # File doesn't exist, create empty config
        config = {}

    config.setdefault("general", {})

    ## general config
    general_settings = data["general"]

    for key, value in general_settings.items():
        config["general"][key] = value

    ## model-specific config
    config.setdefault("model", {})
    config["model"].setdefault(user_model, {})

    user_model_config = data[user_model]
    model_key = model_key = user_model_config.pop("alias", user_model)
    config["model"].setdefault(model_key, {})
    for key, value in user_model_config.items():
        config["model"][model_key][key] = value

    # Write config to file
    with open(user_config_path, "wb") as f:
        tomli_w.dump(config, f)


def load_router_config(router: Optional[litellm.Router], config_file_path: str):
    global master_key
    config = {}
    server_settings: dict = {} 
    try: 
        if os.path.exists(config_file_path):
            with open(config_file_path, 'r') as file:
                config = yaml.safe_load(file)
        else:
            raise Exception(f"Path to config does not exist, 'os.path.exists({config_file_path})' returned False")
    except Exception as e:
        raise Exception(f"Exception while reading Config: {e}")
    
    print(f"Loaded config YAML:\n{json.dumps(config, indent=2)}")

    ## GENERAL SERVER SETTINGS (e.g. master key,..)
    general_settings = config.get("general_settings", None)
    if general_settings: 
        master_key = general_settings.get("master_key", None)

    ## LITELLM MODULE SETTINGS (e.g. litellm.drop_params=True,..)
    litellm_settings = config.get('litellm_settings', None)
    if litellm_settings: 
        for key, value in litellm_settings.items(): 
            setattr(litellm, key, value)

    ## MODEL LIST
    model_list = config.get('model_list', None)
    if model_list:
        router = litellm.Router(model_list=model_list)
        print(f"\033[32mLiteLLM: Proxy initialized with Config, Set models:\033[0m")
        for model in model_list:
            print(f"\033[32m    {model.get('model_name', '')}\033[0m")
        print()

    ## ENVIRONMENT VARIABLES
    environment_variables = config.get('environment_variables', None)
    if environment_variables: 
        for key, value in environment_variables.items(): 
            os.environ[key] = value

    return router, model_list, server_settings

def load_config():
    #### DEPRECATED #### 
    try:
        global user_config, user_api_base, user_max_tokens, user_temperature, user_model, local_logging, llm_model_list, llm_router, server_settings
        
        # Get the file extension
        file_extension = os.path.splitext(user_config_path)[1]
        if file_extension.lower() == ".toml":
            # As the .env file is typically much simpler in structure, we use load_dotenv here directly
            with open(user_config_path, "rb") as f:
                user_config = tomllib.load(f)

            ## load keys
            if "keys" in user_config:
                for key in user_config["keys"]:
                    os.environ[key] = user_config["keys"][
                        key
                    ]  # litellm can read keys from the environment
            ## settings
            if "general" in user_config:
                litellm.add_function_to_prompt = user_config["general"].get(
                    "add_function_to_prompt", True
                )  # by default add function to prompt if unsupported by provider
                litellm.drop_params = user_config["general"].get(
                    "drop_params", True
                )  # by default drop params if unsupported by provider
                litellm.model_fallbacks = user_config["general"].get(
                    "fallbacks", None
                )  # fallback models in case initial completion call fails
                default_model = user_config["general"].get(
                    "default_model", None
                )  # route all requests to this model.

                local_logging = user_config["general"].get("local_logging", True)

                if user_model is None:  # `litellm --model <model-name>`` > default_model.
                    user_model = default_model

            ## load model config - to set this run `litellm --config`
            model_config = None
            if "model" in user_config:
                if user_model in user_config["model"]:
                    model_config = user_config["model"][user_model]
                model_list = []
                for model in user_config["model"]:
                    if "model_list" in user_config["model"][model]:
                        model_list.extend(user_config["model"][model]["model_list"])

            print_verbose(f"user_config: {user_config}")
            print_verbose(f"model_config: {model_config}")
            print_verbose(f"user_model: {user_model}")
            if model_config is None:
                return

            user_max_tokens = model_config.get("max_tokens", None)
            user_temperature = model_config.get("temperature", None)
            user_api_base = model_config.get("api_base", None)

            ## custom prompt template
            if "prompt_template" in model_config:
                model_prompt_template = model_config["prompt_template"]
                if (
                    len(model_prompt_template.keys()) > 0
                ):  # if user has initialized this at all
                    litellm.register_prompt_template(
                        model=user_model,
                        initial_prompt_value=model_prompt_template.get(
                            "MODEL_PRE_PROMPT", ""
                        ),
                        roles={
                            "system": {
                                "pre_message": model_prompt_template.get(
                                    "MODEL_SYSTEM_MESSAGE_START_TOKEN", ""
                                ),
                                "post_message": model_prompt_template.get(
                                    "MODEL_SYSTEM_MESSAGE_END_TOKEN", ""
                                ),
                            },
                            "user": {
                                "pre_message": model_prompt_template.get(
                                    "MODEL_USER_MESSAGE_START_TOKEN", ""
                                ),
                                "post_message": model_prompt_template.get(
                                    "MODEL_USER_MESSAGE_END_TOKEN", ""
                                ),
                            },
                            "assistant": {
                                "pre_message": model_prompt_template.get(
                                    "MODEL_ASSISTANT_MESSAGE_START_TOKEN", ""
                                ),
                                "post_message": model_prompt_template.get(
                                    "MODEL_ASSISTANT_MESSAGE_END_TOKEN", ""
                                ),
                            },
                        },
                        final_prompt_value=model_prompt_template.get(
                            "MODEL_POST_PROMPT", ""
                        ),
                    )
    except:
        pass

def save_worker_config(**data): 
    import json
    os.environ["WORKER_CONFIG"] = json.dumps(data)

def initialize(
    model,
    alias,
    api_base,
    api_version,
    debug,
    temperature,
    max_tokens,
    request_timeout,
    max_budget,
    telemetry,
    drop_params,
    add_function_to_prompt,
    headers,
    save,
    config
):
    global user_model, user_api_base, user_debug, user_max_tokens, user_request_timeout, user_temperature, user_telemetry, user_headers, experimental, llm_model_list, llm_router, server_settings
    generate_feedback_box()
    user_model = model
    user_debug = debug
    dynamic_config = {"general": {}, user_model: {}}
    if config:
        llm_router, llm_model_list, server_settings = load_router_config(router=llm_router, config_file_path=config)
    if headers:  # model-specific param
        user_headers = headers
        dynamic_config[user_model]["headers"] = headers
    if api_base:  # model-specific param
        user_api_base = api_base
        dynamic_config[user_model]["api_base"] = api_base
    if api_version:
        os.environ[
            "AZURE_API_VERSION"
        ] = api_version  # set this for azure - litellm can read this from the env
    if max_tokens:  # model-specific param
        user_max_tokens = max_tokens
        dynamic_config[user_model]["max_tokens"] = max_tokens
    if temperature:  # model-specific param
        user_temperature = temperature
        dynamic_config[user_model]["temperature"] = temperature
    if request_timeout:
        user_request_timeout = request_timeout
        dynamic_config[user_model]["request_timeout"] = request_timeout
    if alias:  # model-specific param
        dynamic_config[user_model]["alias"] = alias
    if drop_params == True:  # litellm-specific param
        litellm.drop_params = True
        dynamic_config["general"]["drop_params"] = True
    if add_function_to_prompt == True:  # litellm-specific param
        litellm.add_function_to_prompt = True
        dynamic_config["general"]["add_function_to_prompt"] = True
    if max_budget:  # litellm-specific param
        litellm.max_budget = max_budget
        dynamic_config["general"]["max_budget"] = max_budget
    if debug==True:  # litellm-specific param
        litellm.set_verbose = True
    if experimental: 
        pass
    if save:
        save_params_to_config(dynamic_config)
        with open(user_config_path) as f:
            print(f.read())
        print("\033[1;32mDone successfully\033[0m")
    user_telemetry = telemetry
    usage_telemetry(feature="local_proxy_server")

# for streaming
def data_generator(response):
    print_verbose("inside generator")
    for chunk in response:
        print_verbose(f"returned chunk: {chunk}")
        try:
            yield f"data: {json.dumps(chunk.dict())}\n\n"
        except:
            yield f"data: {json.dumps(chunk)}\n\n"


def litellm_completion(*args, **kwargs):
    global user_temperature, user_request_timeout, user_max_tokens, user_api_base
    call_type = kwargs.pop("call_type")
    # override with user settings, these are params passed via cli
    if user_temperature: 
        kwargs["temperature"] = user_temperature
    if user_request_timeout:
        kwargs["request_timeout"] = user_request_timeout
    if user_max_tokens: 
        kwargs["max_tokens"] = user_max_tokens
    if user_api_base: 
        kwargs["api_base"] = user_api_base
    ## ROUTE TO CORRECT ENDPOINT ## 
    router_model_names = [m["model_name"] for m in llm_model_list] if llm_model_list is not None else []
    try:
        if llm_router is not None and kwargs["model"] in router_model_names: # model in router model list 
            if call_type == "chat_completion":
                response = llm_router.completion(*args, **kwargs)
            elif call_type == "text_completion":
                response = llm_router.text_completion(*args, **kwargs)
        else: 
            if call_type == "chat_completion":
                response = litellm.completion(*args, **kwargs)
            elif call_type == "text_completion":
                response = litellm.text_completion(*args, **kwargs)
    except Exception as e:
        raise e
    if 'stream' in kwargs and kwargs['stream'] == True: # use generate_responses to stream responses
        return StreamingResponse(data_generator(response), media_type='text/event-stream')
    return response


@app.on_event("startup")
def startup_event():
    import json
    worker_config = json.loads(os.getenv("WORKER_CONFIG"))
    initialize(**worker_config)
    # print(f"\033[32mWorker Initialized\033[0m\n")

#### API ENDPOINTS ####
@router.get("/v1/models", dependencies=[Depends(user_api_key_auth)])
@router.get("/models", dependencies=[Depends(user_api_key_auth)])  # if project requires model list
def model_list():
    global llm_model_list, server_settings    
    all_models = []
    if server_settings.get("infer_model_from_keys", False):
        all_models = litellm.utils.get_valid_models()
    if llm_model_list: 
        all_models += llm_model_list
    if user_model is not None:
        all_models += user_model
    ### CHECK OLLAMA MODELS ### 
    try:
        response = requests.get("http://0.0.0.0:11434/api/tags")
        models = response.json()["models"]
        ollama_models = [m["name"].replace(":latest", "") for m in models]
        all_models.extend(ollama_models)
    except Exception as e: 
        traceback.print_exc()
    return dict(
        data=[
            {
                "id": model,
                "object": "model",
                "created": 1677610602,
                "owned_by": "openai",
            }
            for model in all_models
        ],
        object="list",
    )

@router.post("/v1/completions", dependencies=[Depends(user_api_key_auth)])
@router.post("/completions", dependencies=[Depends(user_api_key_auth)])
@router.post("/engines/{model:path}/completions", dependencies=[Depends(user_api_key_auth)])
async def completion(request: Request, model: Optional[str] = None):
    try: 
        body = await request.body()
        body_str = body.decode()
        try:
            data = ast.literal_eval(body_str)
        except: 
            data = json.loads(body_str)
        data["model"] = (
            server_settings.get("completion_model", None) # server default
            or user_model # model name passed via cli args
            or model # for azure deployments
            or data["model"] # default passed in http request
        )
        if user_model:
            data["model"] = user_model
        data["call_type"] = "text_completion"
        return litellm_completion(
            **data
        )
    except Exception as e: 
        print(f"\033[1;31mAn error occurred: {e}\n\n Debug this by setting `--debug`, e.g. `litellm --model gpt-3.5-turbo --debug`")
        error_traceback = traceback.format_exc()
        error_msg = f"{str(e)}\n\n{error_traceback}"
        try:
            status = e.status_code  # type: ignore
        except:
            status = status.HTTP_500_INTERNAL_SERVER_ERROR,
        raise HTTPException(
            status_code=status,
            detail=error_msg
        )
                              

@router.post("/v1/chat/completions", dependencies=[Depends(user_api_key_auth)])
@router.post("/chat/completions", dependencies=[Depends(user_api_key_auth)])
@router.post("/openai/deployments/{model:path}/chat/completions", dependencies=[Depends(user_api_key_auth)]) # azure compatible endpoint
async def chat_completion(request: Request, model: Optional[str] = None):
    global server_settings
    try: 
        body = await request.body()
        body_str = body.decode()
        try:
            data = ast.literal_eval(body_str)
        except: 
            data = json.loads(body_str)
        data["model"] = (
            server_settings.get("completion_model", None) # server default
            or user_model # model name passed via cli args
            or model # for azure deployments
            or data["model"] # default passed in http request
        )
        data["call_type"] = "chat_completion"
        return litellm_completion(
            **data
        )
    except Exception as e: 
        print(f"\033[1;31mAn error occurred: {e}\n\n Debug this by setting `--debug`, e.g. `litellm --model gpt-3.5-turbo --debug`")
        error_traceback = traceback.format_exc()
        error_msg = f"{str(e)}\n\n{error_traceback}"
        try:
            status = e.status_code # type: ignore
        except:
            status = status.HTTP_500_INTERNAL_SERVER_ERROR,
        raise HTTPException(
            status_code=status,
            detail=error_msg
        )


@router.post("/router/chat/completions", dependencies=[Depends(user_api_key_auth)])
async def router_completion(request: Request):
    try: 
        body = await request.body()
        body_str = body.decode()
        try:
            data = ast.literal_eval(body_str)
        except: 
            data = json.loads(body_str)
        return {"data": data}
    except Exception as e: 
        print(f"\033[1;31mAn error occurred: {e}\n\n Debug this by setting `--debug`, e.g. `litellm --model gpt-3.5-turbo --debug`")
        error_traceback = traceback.format_exc()
        error_msg = f"{str(e)}\n\n{error_traceback}"
        try:
            status = e.status_code # type: ignore
        except:
            status = status.HTTP_500_INTERNAL_SERVER_ERROR,
        raise HTTPException(
            status_code=status,
            detail=error_msg
        )

@router.get("/ollama_logs", dependencies=[Depends(user_api_key_auth)])
async def retrieve_server_log(request: Request):
    filepath = os.path.expanduser("~/.ollama/logs/server.log")
    return FileResponse(filepath)


@router.get("/")
async def home(request: Request):
    return "LiteLLM: RUNNING"


app.include_router(router)